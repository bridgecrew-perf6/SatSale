from flask import (
    Flask,
    render_template,
    request,
    make_response
)
from flask_restplus import Resource, Api, fields
import time
import os
import uuid
from pprint import pprint
import qrcode
import logging

import config

# Initialise logging before importing other modules
logging.basicConfig(
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S %z",
    level=getattr(logging, config.loglevel),
)

from gateways import paynym
from payments import database, weakhands
from payments.price_feed import get_btc_value
from node import bitcoind
from node import xpub
from node import lnd
from node import clightning
from utils import btc_amount_format

from gateways import woo_webhook

app = Flask(__name__)

# Load a SatSale API key or create a new one
if os.path.exists("SatSale_API_key"):
    with open("SatSale_API_key", "r") as f:
        app.config["SECRET_KEY"] = f.read().strip()
else:
    with open("SatSale_API_key", "w") as f:
        app.config["SECRET_KEY"] = os.urandom(64).hex()
        f.write(app.config["SECRET_KEY"])

logging.info("Initialised Flask with secret key: {}".format(app.config["SECRET_KEY"]))

# Create payment database if it does not exist
if not os.path.exists("database.db"):
    database.create_database()
# Check and migrate database to current version if needed
database.migrate_database()


# Render index page
# This is currently a donation page that submits to /pay
@app.route("/")
def index():
    params = dict(request.args)
    params["currency"] = config.base_currency
    params["node_info"] = config.node_info
    headers = {"Content-Type": "text/html"}
    return make_response(render_template("donate.html", params=params), 200, headers)


# /pay is the main page for initiating a payment, takes a GET request with ?amount=
@app.route("/pay")
def pay():
    params = dict(request.args)
    params["payment_methods"] = enabled_payment_methods
    params["redirect"] = config.redirect
    params["node_info"] = config.node_info
    # Render payment page with the request arguments (?amount= etc.)
    headers = {"Content-Type": "text/html"}
    return make_response(render_template("index.html", params=params), 200, headers)


# Now we build the API docs
# (if you do this before the above @app.routes then / gets overwritten.)
api = Api(
    app,
    version="0.1",
    title="SatSale API",
    default="SatSale /api/",
    description="API for creating Bitcoin invoices and processing payments.",
    doc="/docs/",
    order=True,
)

# Model templates for API responses
invoice_model = api.model(
    "invoice",
    {
        "uuid": fields.String(),
        "fiat_value": fields.Float(),
        "btc_value": fields.Float(),
        "method": fields.String(),
        "address": fields.String(),
        "time": fields.Float(),
        "webhook": fields.String(),
        "rhash": fields.String(),
        "time_left": fields.Float(),
    },
)
status_model = api.model(
    "status",
    {
        "payment_complete": fields.Integer(),
        "confirmed_paid": fields.Float(),
        "unconfirmed_paid": fields.Float(),
        "expired": fields.Integer(),
    },
)


@api.doc(
    params={
        "amount": "An amount in `config.base_currency`.",
        "method": "(Optional) Specify a payment method: `bitcoind` for onchain, `lnd` for lightning).",
        "w_url": "(Optional) Specify a webhook url to call after successful payment. Currently only supports WooCommerce plugin.",
    }
)
class create_payment(Resource):
    @api.response(200, "Success", invoice_model)
    @api.response(400, "Invalid payment method")
    @api.response(406, "Amount below dust limit")
    @api.response(522, "Error fetching address from node")
    def get(self):
        "Create Payment"
        """Initiate a new payment with an `amount` in `config.base_currecy`."""
        base_amount = request.args.get("amount")
        currency = config.base_currency
        payment_method = request.args.get("method")
        if payment_method is None:
            payment_method = enabled_payment_methods[0]
        webhook = request.args.get("w_url")
        if webhook is None:
            webhook = None
        else:
            logging.info("Webhook payment: {}".format(webhook))

        # Create the payment using one of the connected nodes as a base
        # ready to recieve the invoice.
        node = get_node(payment_method)
        if node is None:
            logging.warning("Invalid payment method {}".format(payment_method))
            return {"message": "Invalid payment method."}, 400

        btc_value = get_btc_value(base_amount, currency)
        if node.is_onchain and btc_value < config.onchain_dust_limit:
            logging.warning(
                "Requested onchain payment for {} {} below dust limit ({} < {})".format(
                    base_amount,
                    currency,
                    btc_amount_format(btc_value),
                    btc_amount_format(config.onchain_dust_limit),
                )
            )
            return {"message": "Amount below dust limit."}, 406

        invoice = {
            "uuid": str(uuid.uuid4().hex),
            "fiat_value": base_amount,
            "btc_value": btc_amount_format(btc_value),
            "method": payment_method,
            "time": time.time(),
            "webhook": webhook,
            "onchain_dust_limit": config.onchain_dust_limit,
        }

        # Get an address / invoice, and create a QR code
        try:
            invoice["address"], invoice["rhash"] = node.get_address(
                invoice["btc_value"], invoice["uuid"], config.payment_timeout
            )
        except Exception as e:
            logging.error("Failed to fetch address: {}".format(e))
            return {"message": "Error fetching address. Check config.."}, 522

        node.create_qr(invoice["uuid"], invoice["address"], invoice["btc_value"])

        # Save invoice to database
        database.write_to_database(invoice)

        invoice["time_left"] = config.payment_timeout - (time.time() - invoice["time"])
        logging.info("Created invoice:")
        pprint(invoice)
        print()

        return {"invoice": invoice}, 200


@api.doc(params={"uuid": "A payment uuid. Received from /createpayment."})
class check_payment(Resource):
    @api.response(200, "Success", status_model)
    @api.response(201, "Unconfirmed", status_model)
    @api.response(202, "Payment Expired", status_model)
    def get(self):
        "Check Payment"
        """Check the status of a payment."""
        uuid = request.args.get("uuid")
        status = check_payment_status(uuid)

        response = {
            "payment_complete": 0,
            "confirmed_paid": 0,
            "unconfirmed_paid": 0,
            "expired": 0,
        }

        # If payment is expired
        if status["time_left"] <= 0:
            response.update({"expired": 1})
            code = 202
        else:
            # Update response with confirmed and unconfirmed amounts
            response.update(status)

        # Return whether paid or unpaid
        if response["payment_complete"] == 1:
            code = 200
        else:
            code = 201

        return {"status": response}, code


@api.doc(params={"uuid": "A payment uuid. Received from /createpayment."})
class complete_payment(Resource):
    @api.response(200, "Payment confirmed.")
    @api.response(400, "Payment expired.")
    @api.response(500, "Webhook failure.")
    def get(self):
        "Complete Payment"
        """Run post-payment processing such as any webhooks."""
        uuid = request.args.get("uuid")
        order_id = request.args.get("id")

        invoice = database.load_invoice_from_db(uuid)
        status = check_payment_status(uuid)

        if status["time_left"] < 0:
            return {"message": "Expired."}, 400

        if status["payment_complete"] != 1:
            return {"message": "You havent paid you stingy bastard"}

        if (config.liquid_address is not None) and (
            invoice["method"] in ["lnd", "clightning"]
        ):
            weakhands.swap_lnbtc_for_lusdt(
                lightning_node, invoice["btc_value"], config.liquid_address
            )

        # Call webhook to confirm payment with merchant
        if (invoice["webhook"] is not None) and (invoice["webhook"] != ""):
            logging.info("Calling webhook {}".format(invoice["webhook"]))
            response = woo_webhook.hook(app.config["SECRET_KEY"], invoice, order_id)

            if response.status_code != 200:
                err = "Failed to confirm order payment via webhook {}, please contact the store to ensure the order has been confirmed, error response is: {}".format(
                    response.status_code, response.text
                )
                logging.error(err)
                return {"message": err}, 500

            logging.info("Successfully confirmed payment via webhook.")
            return {"message": "Payment confirmed with store."}, 200

        return {"message": "Payment confirmed."}, 200


def check_payment_status(uuid):
    status = {
        "payment_complete": 0,
        "confirmed_paid": 0,
        "unconfirmed_paid": 0,
    }
    invoice = database.load_invoice_from_db(uuid)
    if invoice is None:
        status.update({"time_left": 0, "not_found": 1})
    else:
        status["time_left"] = config.payment_timeout - (time.time() - invoice["time"])

    # If payment has not expired, then we're going to check for any transactions
    if status["time_left"] > 0:
        node = get_node(invoice["method"])
        if node.config['name'] == "lnd":
            conf_paid, unconf_paid = node.check_payment(invoice["rhash"])
        elif (node.config['name'] == "bitcoind") or (node.config['name'] == "clightning"):
            # Lookup bitcoind / clightning invoice based on label (uuid)
            conf_paid, unconf_paid = node.check_payment(invoice["uuid"])
        elif node.config['name'] == "xpub":
            conf_paid, unconf_paid = node.check_payment(invoice["address"])

        # Remove any Decimal types
        conf_paid, unconf_paid = float(conf_paid), float(unconf_paid)

        # Debugging and demo mode which auto confirms payments after 5 seconds
        dbg_free_mode_cond = config.free_mode and (time.time() - invoice["time"] > 5)

        # If payment is paid
        if (conf_paid >= float(invoice["btc_value"]) - config.allowed_underpay_amount) or dbg_free_mode_cond:
            status.update(
                {
                    "payment_complete": 1,
                    "confirmed_paid": btc_amount_format(conf_paid),
                    "unconfirmed_paid": btc_amount_format(unconf_paid),
                }
            )
        else:
            status.update(
                {
                    "payment_complete": 0,
                    "confirmed_paid": btc_amount_format(conf_paid),
                    "unconfirmed_paid": btc_amount_format(unconf_paid),
                }
            )

    logging.debug("Invoice {} status: {}".format(uuid, status))
    return status


def get_node(payment_method):
    if payment_method == "onchain":
        node = bitcoin_node
    elif payment_method == "lightning":
        node = lightning_node
    else:
        node = None
    return node


# Add API endpoints
api.add_resource(create_payment, "/api/createpayment")
api.add_resource(check_payment, "/api/checkpayment")
api.add_resource(complete_payment, "/api/completepayment")

# Test connections on startup:
enabled_payment_methods = []
for method in config.payment_methods:
    #print(method)
    if method['name'] == "bitcoind":
        bitcoin_node = bitcoind.btcd(method)
        logging.info("Connection to bitcoin node successful.")
        enabled_payment_methods.append("onchain")

    elif method['name'] == "lnd":
        lightning_node = lnd.lnd(method)
        logging.info("Connection to lightning node (lnd) successful.")
        if lightning_node.config['lightning_address'] is not None:
            from gateways import lightning_address
            lightning_address.add_ln_address_decorators(app, api, lightning_node)
        enabled_payment_methods.append("lightning")

    elif method['name'] == "clightning":
        lightning_node = clightning.clightning(method)
        logging.info("Connection to lightning node (clightning) successful.")
        enabled_payment_methods.append("lightning")

    elif method['name'] == "xpub":
        bitcoin_node = xpub.xpub(method)
        logging.info("Not connecting to a bitcoin node, using xpubs and blockexplorer APIs.")
        enabled_payment_methods.append("onchain")

# Add node connection page
if config.node_info is not None:
    @app.route("/node/")
    def node():
        if config.node_info is True:
            uri = lightning_node.get_uri()
        else:
            uri = config.node_info
        img = qrcode.make(uri)
        img.save("static/qr_codes/node.png")
        headers = {"Content-Type": "text/html"}
        return make_response(
            render_template("node.html", params={"uri": uri}), 200, headers
        )

# Add lightning address
try:
    if lightning_node.config['lightning_address'] is not None:
        from gateways import lightning_address
        lightning_address.add_ln_address_decorators(app, api, lightning_node)
except NameError:
    # lightning_node is not defined if no LN support configured
    pass

# Add Paynym
if config.paynym is not None:
    paynym.insert_paynym_html(config.paynym)

if __name__ == "__main__":
    app.run(debug=False)
