import os
import logging
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from app.utils.filters import format_date, nl2br, accounting, accounting_number
from app.routes import email_bp
from app.blueprints.accounts import accounts_bp
from app.blueprints.expediting import bp as expediting_bp
from dotenv import load_dotenv

# Initialize SQLAlchemy
db = SQLAlchemy()

def create_app():
    # Load environment variables from .env
    load_dotenv()

    app = Flask(__name__)

    # Basic Flask config
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret")
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///po_system.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Supabase config
    app.config["SUPABASE_URL"] = os.getenv("SUPABASE_URL")
    app.config["SUPABASE_API_KEY"] = os.getenv("SUPABASE_KEY")

    # Init extensions
    db.init_app(app)
    CORS(app)

    # 🔽 Enable stdout logging (critical for Docker)
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
    if not app.debug:
        app.logger.setLevel(logging.DEBUG)

    # 🔽 Optional: disable print buffering globally
    import sys
    sys.stdout.reconfigure(line_buffering=True)

    # Register routes
    from .routes import main
    app.register_blueprint(main)
    app.register_blueprint(email_bp)
    app.register_blueprint(accounts_bp)
    app.register_blueprint(expediting_bp)

    # other setup...
    app.jinja_env.filters["format_date"] = format_date
    app.jinja_env.filters["nl2br"] = nl2br
    app.jinja_env.filters["accounting"] = accounting
    app.jinja_env.filters["accounting_number"] = accounting_number
    
    return app
