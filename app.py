import pytz
from models.database import db
from models.admin import  admin_bp, create_super_admin, WebsiteSettings, BlogCategory, Blog, WebStory
import traceback
from models.user import User, EmailLog
from models.contact import ContactSubmission
from models.subscription import (
    Subscription, SubscribedUser, subscription_required, 
    has_active_subscription, increment_qr_usage, design_access_required,
    has_design_access
)
from flask import g  # For storing subscription info during request
from datetime import datetime, UTC
from models.payment import Payment

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import ForeignKey, case, or_, func
from sqlalchemy.orm import relationship, joinedload
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from urllib.parse import urlparse
from PIL import ImageChops, Image  
import qrcode
from io import BytesIO
import base64
import os
import re
from flask_migrate import Migrate
import razorpay
from decimal import Decimal, ROUND_HALF_UP
from flask_caching import Cache
import uuid
from models.qr_models import QRCode, Scan, QREmail, QRPhone, QRSms, QRWhatsApp, QRWifi, QRVCard, QREvent, QRText, QRLink, QRImage

import logging
from datetime import datetime, UTC, timedelta
from flask_mail import Mail, Message
from flask_wtf.csrf import CSRFProtect
import json
import requests
from utils.email_service import email_service, init_email_service
from PIL import Image, ImageDraw, ImageFont
import math
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.moduledrawers import (
    SquareModuleDrawer,
    RoundedModuleDrawer, 
    CircleModuleDrawer,
    VerticalBarsDrawer,
    GappedSquareModuleDrawer,
    HorizontalBarsDrawer
)
from flask_wtf.csrf import CSRFError
from qrcode.image.styles.colormasks import SolidFillColorMask
from itsdangerous import URLSafeTimedSerializer as Serializer
from flask_wtf.csrf import CSRFProtect

from flask_wtf.csrf import CSRFProtect
from flask import (
    Flask, render_template, request, redirect, send_file,
    url_for, flash, jsonify, session, current_app, g
)


from models.subscription import subscription_bp

# Load environment variables
from dotenv import load_dotenv
import os
# Load .env file from the same directory as app.py
dotenv_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(dotenv_path, override=True)
print(f"DEBUG: Loading .env from: {dotenv_path}")
print(f"DEBUG: DB URI = {os.getenv('SQLALCHEMY_DATABASE_URI')}")

# Create the Flask instance at module level
app = Flask(__name__)

# Initialize Flask-Mail and Flask-Login
mail = Mail()
login_manager = LoginManager()

def format_amount(value):
    """Format amount with 2 decimal places"""
    try:
        return "{:.2f}".format(float(value))
    except (ValueError, TypeError):
        return "0.00"

def from_json(value):
    """Convert JSON string to Python object"""
    try:
        if value:
            return json.loads(value)
        return []
    except (json.JSONDecodeError, TypeError):
        return []

app = Flask(__name__)

# Register custom filters and globals
app.jinja_env.filters['format_amount'] = format_amount
app.jinja_env.filters['from_json'] = from_json
app.jinja_env.globals['hasattr'] = hasattr  # Add hasattr as a global



def create_app():
    # Configure the app using environment variables
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'fallback-secret-key-for-development')
    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('SQLALCHEMY_DATABASE_URI', 'sqlite:///qr_codes.db')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = os.getenv('SQLALCHEMY_TRACK_MODIFICATIONS', 'False').lower() == 'true'

    # Template auto-reload configuration (important for AWS deployment)
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
    # Update your database URI configuration
    # app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    #     'pool_recycle': 300,
    #     'pool_pre_ping': True,
    #     'pool_timeout': 20,
    #     'max_overflow': 0
    # }  
     # FIXED: Ensure upload folder configuration
    upload_folder = os.getenv('UPLOAD_FOLDER', 'static/uploads')
    app.config['UPLOAD_FOLDER'] = upload_folder

    # Set maximum file upload size (20MB)
    app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20MB in bytes

    # Create upload directories
    os.makedirs(upload_folder, exist_ok=True)
    os.makedirs(os.path.join(upload_folder, 'logos'), exist_ok=True)
    os.makedirs(os.path.join(upload_folder, 'temp'), exist_ok=True)
    os.makedirs(os.path.join(upload_folder, 'vcard'), exist_ok=True)
    os.makedirs(os.path.join(upload_folder, 'qr_images'), exist_ok=True)

    print(f"Upload directories created: {upload_folder}")

    # Initialize the shared SQLAlchemy instance with this app
    db.init_app(app)

    # Create tables within app context
    with app.app_context():
        db.create_all()

        # Add gradient columns to qr_image table if missing
        from sqlalchemy import inspect as sa_inspect, text
        try:
            insp = sa_inspect(db.engine)
            if insp.has_table('qr_image'):
                existing_cols = [c['name'] for c in insp.get_columns('qr_image')]
                with db.engine.connect() as conn:
                    if 'bg_color_1' not in existing_cols:
                        conn.execute(text("ALTER TABLE qr_image ADD COLUMN bg_color_1 VARCHAR(20) DEFAULT '#0a0a0a'"))
                    if 'bg_color_2' not in existing_cols:
                        conn.execute(text("ALTER TABLE qr_image ADD COLUMN bg_color_2 VARCHAR(20) DEFAULT '#1a1a2e'"))
                    if 'bg_direction' not in existing_cols:
                        conn.execute(text("ALTER TABLE qr_image ADD COLUMN bg_direction VARCHAR(30) DEFAULT 'to bottom'"))
                    conn.commit()
        except Exception as e:
            print(f"Migration check for qr_image: {e}")

    # Register blueprints with proper URL prefixes
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(subscription_bp, url_prefix='/subscription')

    app.config['DEFAULT_TIMEZONE'] = os.getenv('DEFAULT_TIMEZONE', 'Asia/Calcutta')
    
    #----------------------
    # CSRF Protection
    #----------------------
    app.config['WTF_CSRF_ENABLED'] = os.getenv('WTF_CSRF_ENABLED', 'False').lower() == 'true'
    app.config['WTF_CSRF_SECRET_KEY'] = os.getenv('WTF_CSRF_SECRET_KEY', os.urandom(24))
    app.config['WTF_CSRF_TIME_LIMIT'] = int(os.getenv('WTF_CSRF_TIME_LIMIT', '3600'))

    csrf = CSRFProtect(app)

    # Session Security Configuration
    app.config['SESSION_COOKIE_SECURE'] = os.getenv('SESSION_COOKIE_SECURE', 'True').lower() == 'true'
    app.config['SESSION_COOKIE_HTTPONLY'] = os.getenv('SESSION_COOKIE_HTTPONLY', 'True').lower() == 'true'
    app.config['SESSION_COOKIE_SAMESITE'] = os.getenv('SESSION_COOKIE_SAMESITE', 'Lax')
    
    #Selective Route Protection
    @app.before_request
    def csrf_protect():
        """
        Conditionally apply CSRF protection only to authentication routes
        
        Mental Model: 
        - Like a selective security checkpoint
        - Only validates tokens for specific, sensitive routes
        """
        # if request.method == "POST":
            # List of routes that require CSRF protection
            # protected_routes = [
                # 'login', 
                # 'signup', 
                # 'reset_token', 
                # 'reset_request', 
                # 'resend_verification'
            # ]
            
            # Check if current route needs protection
            # if request.endpoint in protected_routes:
                # csrf.protect()
              
    def init_app():
        with app.app_context():
            db.create_all()
            create_super_admin()

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        """
        Provide clear, user-friendly error handling for CSRF token failures
        
        Key Principles:
        - Log the security event
        - Inform user without revealing sensitive details
        - Redirect to a safe page
        """
        # Log the security event for monitoring
        app.logger.warning(
            f"CSRF Token Validation Failed: "
            f"Route: {request.endpoint}, "
            f"Method: {request.method}"
        )
        
        # User-friendly error message
        flash(
            'Your form submission was invalid. Please try again. '
            'If the problem persists, clear your browser cookies and reload the page.', 
            'danger'
        )
        
        # Context-aware redirection
        if request.endpoint == 'login':
            return redirect(url_for('login'))
        elif request.endpoint == 'signup':
            return redirect(url_for('signup'))
        
        return redirect(url_for('index'))
        
    #----------------------
    # Logging configuration
    #----------------------
    log_file = os.getenv('LOG_FILE', 'flask_app.log')
    log_level = getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper())
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), log_file)
    
    logging.basicConfig(
        filename=log_path, 
        level=log_level, 
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    logging.info("Flask app started successfully with environment configuration")

    # Ensure download directory exists
    download_dir = os.getenv('DOWNLOAD_DIRECTORY', 'download_files')
    os.makedirs(download_dir, exist_ok=True)

    # Configure Flask-Caching
    app.config['CACHE_TYPE'] = os.getenv('CACHE_TYPE', 'simple')
    app.config['CACHE_DEFAULT_TIMEOUT'] = int(os.getenv('CACHE_DEFAULT_TIMEOUT', '300'))
    cache = Cache(app)

    # Razorpay configuration using environment variables
    app.config['RAZORPAY_KEY_ID'] = os.getenv('RAZORPAY_KEY_ID')
    app.config['RAZORPAY_KEY_SECRET'] = os.getenv('RAZORPAY_KEY_SECRET')
    app.config['RAZORPAY_WEBHOOK_SECRET'] = os.getenv('RAZORPAY_WEBHOOK_SECRET')

    # Initialize Razorpay client only if credentials are provided
    if app.config['RAZORPAY_KEY_ID'] and app.config['RAZORPAY_KEY_SECRET']:
        razorpay_client = razorpay.Client(auth=(app.config['RAZORPAY_KEY_ID'], app.config['RAZORPAY_KEY_SECRET']))
        app.config['RAZORPAY_CLIENT'] = razorpay_client
    else:
        logging.warning("Razorpay credentials not found in environment variables")

    # Flask-Mail configuration using environment variables (Legacy - kept for compatibility)
    app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
    app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', '587'))
    app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'True').lower() == 'true'
    app.config['MAIL_USE_SSL'] = os.getenv('MAIL_USE_SSL', 'False').lower() == 'true'
    app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME', 'support@qrdada.com')
    app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD', '')

    # Initialize new OAuth2 email service
    init_email_service(app)
    print("✓ Email service initialized with OAuth2 support")
    print("  - payment@qrdada.com: For payment confirmations")
    print("  - support@qrdada.com: For verification, password reset, and support")

    # Initialize Flask-Mail (kept for compatibility but will use OAuth2 wrapper)
    mail = Mail(app)
    mail.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'login'
    login_manager.login_message = 'You need to log in to access this page.'
    login_manager.login_message_category = 'info'
    migrate = Migrate(app, db)

    # Initialize scheduler for automated tasks
    try:
        from scheduler import init_scheduler
        init_scheduler(app)
        print("✓ Scheduler initialized - Subscription expiry notifications will run automatically")
    except Exception as e:
        print(f"Warning: Failed to initialize scheduler: {str(e)}")
        logging.warning(f"Scheduler initialization failed: {str(e)}")

# 1. REPLACE your existing QR_TEMPLATES dictionary with this updated version:

# FIXED: QR_TEMPLATES with proper frame clearing for non-corporate templates
# Replace your existing QR_TEMPLATES dictionary with this fixed version

QR_TEMPLATES = {
    "modern": {
        "shape": "rounded",
        "color": "#2c5282",
        "background_color": "#FFFFFF",
        "custom_eyes": True,
        "inner_eye_style": "circle",
        "outer_eye_style": "rounded",
        "inner_eye_color": "#2c5282",
        "outer_eye_color": "#2c5282",
        "export_type": "png",
        "gradient": False,
        "using_gradient": False,
        # CRITICAL: Explicitly clear frame settings
        "frame_type": None,
        "frame_color": None,
        "frame_text": None
    },
    "corporate": {
        "shape": "square",
        "color": "#000000",
        "background_color": "#FFFFFF",
        "frame_type": "square",
        "frame_color": "#000000",
        "frame_text": "SCAN ME",
        "custom_eyes": True,
        "inner_eye_style": "square",
        "outer_eye_style": "square",
        "inner_eye_color": "#000000",
        "outer_eye_color": "#000000",
        "export_type": "png",
        "gradient": False,
        "using_gradient": False
    },
    "playful": {
        "shape": "circle",
        "background_color": "#FFFFFF",
        "export_type": "gradient",
        "gradient": True,
        "using_gradient": True,
        "gradient_start": "#f97316",
        "gradient_end": "#fbbf24",
        "gradient_type": "linear",
        "gradient_direction": "to-right",
        "custom_eyes": True,
        "inner_eye_style": "circle",
        "outer_eye_style": "circle",
        "inner_eye_color": "#f97316",
        "outer_eye_color": "#fbbf24",
        # CRITICAL: Explicitly clear frame settings
        "frame_type": None,
        "frame_color": None,
        "frame_text": None
    },
    "minimal": {
        "shape": "square",
        "color": "#2d3748",
        "background_color": "#FFFFFF",
        "custom_eyes": True,
        "inner_eye_style": "square",
        "outer_eye_style": "square",
        "inner_eye_color": "#2d3748",
        "outer_eye_color": "#2d3748",
        "export_type": "png",
        "gradient": False,
        "using_gradient": False,
        # CRITICAL: Explicitly clear frame settings
        "frame_type": None,
        "frame_color": None,
        "frame_text": None
    },
    "high_contrast": {
        "shape": "square",
        "color": "#000000",
        "background_color": "#FFFFFF",
        "module_size": 12,
        "quiet_zone": 4,
        "custom_eyes": True,
        "inner_eye_style": "square",
        "outer_eye_style": "square",
        "inner_eye_color": "#000000",
        "outer_eye_color": "#000000",
        "export_type": "png",
        "gradient": False,
        "using_gradient": False,
        # CRITICAL: Explicitly clear frame settings
        "frame_type": None,
        "frame_color": None,
        "frame_text": None
    }
}

@app.before_request
def load_subscription_data():
    """Load user's subscription data for the current request"""
    if 'user_id' in session:
        user_id = session.get('user_id')
        # Get active subscription
        active_subscription = (
            SubscribedUser.query
            .filter(SubscribedUser.U_ID == user_id)
            .filter(SubscribedUser.end_date > datetime.now(UTC))
            .filter(SubscribedUser._is_active == True)
            .join(Subscription, SubscribedUser.S_ID == Subscription.S_ID)
            .first()
        )
        
        if active_subscription:
            # Store subscription info in g object for access during request
            g.subscription = active_subscription
            g.subscription_plan = active_subscription.subscription
            g.has_subscription = True
            g.qr_remaining = active_subscription.get_qr_remaining()
            g.analytics_remaining = active_subscription.get_analytics_remaining()
            g.subscription_tier = active_subscription.subscription.tier
            g.available_designs = active_subscription.subscription.get_designs()
        else:
            g.has_subscription = False
            g.qr_remaining = 0
            g.analytics_remaining = 0
            g.subscription_tier = 0
            g.available_designs = []

# Custom Module Drawer Classes
class DiamondModuleDrawer(SquareModuleDrawer):
    """Custom drawer that draws diamond shapes for modules"""
    
    def drawrect(self, box, is_active):
        """Draw a diamond shape for the module."""
        if not is_active:
            return
        
        x, y, w, h = box
        cx, cy = x + w/2, y + h/2
        size = min(w, h)
        d = size / 2
        
        # Create a diamond shape
        self.draw.polygon([
            (cx, cy - d),  # Top
            (cx + d, cy),  # Right
            (cx, cy + d),  # Bottom
            (cx - d, cy)   # Left
        ], fill=self.color)

class CrossModuleDrawer(SquareModuleDrawer):
    """Custom drawer that draws X/cross shapes for modules"""
    
    def drawrect(self, box, is_active):
        """Draw an X shape for the module."""
        if not is_active:
            return
        
        x, y, w, h = box
        thickness = min(w, h) / 4
        
        # Draw the X shape
        self.draw.polygon([
            (x, y), (x + thickness, y), 
            (x + w, y + h - thickness), (x + w, y + h),
            (x + w - thickness, y + h), (x, y + thickness)
        ], fill=self.color)
        
        self.draw.polygon([
            (x + w, y), (x + w, y + thickness),
            (x + thickness, y + h), (x, y + h),
            (x, y + h - thickness), (x + w - thickness, y)
        ], fill=self.color)



def get_module_drawer(shape):
    """Get the appropriate module drawer based on shape name with improved support for round and circular shapes"""
    try:
        shapes = {
            'square': SquareModuleDrawer(),
            'rounded': RoundedModuleDrawer(radius_ratio=0.5),  # Increased radius ratio for better rounding
            'circle': CircleModuleDrawer(),
            'vertical_bars': VerticalBarsDrawer(),
            'horizontal_bars': HorizontalBarsDrawer(),
            'gapped_square': GappedSquareModuleDrawer()  # Removed the gap_width parameter
        }
        return shapes.get(shape, SquareModuleDrawer())
    except Exception as e:
        print(f"Error getting module drawer: {str(e)}")
        return SquareModuleDrawer()
# Enhanced Subscription Model
@app.template_filter('nl2br')
def nl2br(value):
    """Convert newlines to HTML line breaks."""
    if value:
        return value.replace('\n', '<br>')
    return value

@app.errorhandler(404)
def page_not_found(e):
    return render_template('error.html', 
                          error_code=404,
                          error_message="QR Code not found"), 404

@app.errorhandler(500)
def internal_server_error(e):
    return render_template('error.html', 
                          error_code=500,
                          error_message="An unexpected error occurred"), 500

# QUICK FIX - Add this single filter to your app.py file
# Place it after your Flask app initialization (after app = Flask(__name__))
# and before your routes


@app.template_filter('basename')
def basename_filter(path):
    """Extract filename from a file path"""
    if not path:
        return ''
    try:
        return os.path.basename(path)
    except (TypeError, AttributeError):
        return str(path)

# That's it! This will fix the "No filter named 'basename' found" error

# Update the date_filter to use the improved localization:

@app.template_filter('date')
def date_filter(value, format='%Y-%m-%d'):
    """
    Custom Jinja2 filter to format datetime objects with timezone support
    """
    return localize_datetime_filter(value, format)

# ----------------------
# Login Required Decorator
# ----------------------
from functools import wraps

def login_required(f):
    @wraps(f)  # Preserve function metadata
    def wrap(*args, **kwargs):
        if 'user_id' not in session:
            flash("You need to log in first.", "warning")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrap

# ----------------------
#custom email validation
# ----------------------
# Function to send email verification
def send_verification_email(user):
    """Send email verification with logging"""
    try:
        token = user.get_email_confirm_token()
        subject = 'Email Verification - QR Dada'
        verification_url = url_for('verify_email', token=token, _external=True)
        logo_url = "https://qrdada.com/static/images/qr.png"

        # Plain text version
        body = f'''Hi {user.name},

Thank you for signing up with QR Dada!

To verify your email address, please click the following link:
{verification_url}

This link will expire in 24 hours.

If you did not create an account, please ignore this email.

Thanks,
The QR Dada Team
'''

        # HTML version
        html = f'''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Email Verification - QR Dada</title>
</head>
<body style="margin: 0; padding: 0; font-family: 'Inter', 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f8fafc;">
    <table role="presentation" style="width: 100%; border-collapse: collapse; background-color: #f8fafc;">
        <tr>
            <td align="center" style="padding: 40px 20px;">
                <table role="presentation" style="width: 100%; max-width: 600px; border-collapse: collapse; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);">
                    <!-- Logo Header -->
                    <tr>
                        <td align="center" style="padding: 40px 40px 20px 40px; background: linear-gradient(-45deg, #0ea5e9, #8b5cf6); border-radius: 12px 12px 0 0;">
                            <img src="{logo_url}" alt="QR Dada Logo" style="width: 80px; height: auto; display: block; background-color: #ffffff; padding: 10px; border-radius: 8px;">
                        </td>
                    </tr>

                    <!-- Main Content -->
                    <tr>
                        <td style="padding: 40px;">
                            <h1 style="margin: 0 0 20px 0; font-size: 28px; font-weight: 700; color: #1e293b; text-align: center;">
                                Verify Your Email Address
                            </h1>

                            <p style="margin: 0 0 20px 0; font-size: 16px; line-height: 1.6; color: #475569;">
                                Hi <strong>{user.name}</strong>,
                            </p>

                            <p style="margin: 0 0 20px 0; font-size: 16px; line-height: 1.6; color: #475569;">
                                Thank you for signing up with QR Dada! To complete your registration and start creating amazing QR codes, please verify your email address by clicking the button below.
                            </p>

                            <!-- CTA Button -->
                            <table role="presentation" style="width: 100%; border-collapse: collapse; margin: 30px 0;">
                                <tr>
                                    <td align="center">
                                        <a href="{verification_url}" style="display: inline-block; padding: 16px 40px; background: linear-gradient(-45deg, #0ea5e9, #8b5cf6); color: #ffffff; text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 16px; box-shadow: 0 4px 6px rgba(124, 58, 237, 0.3);">
                                            Verify Email Address
                                        </a>
                                    </td>
                                </tr>
                            </table>

                            <p style="margin: 20px 0; font-size: 14px; line-height: 1.6; color: #64748b; text-align: center;">
                                Or copy and paste this link into your browser:
                            </p>

                            <p style="margin: 0 0 20px 0; font-size: 13px; line-height: 1.6; color: #8b5cf6; word-break: break-all; text-align: center; background-color: #f1f5f9; padding: 12px; border-radius: 6px;">
                                {verification_url}
                            </p>

                            <div style="margin: 30px 0; padding: 20px; background-color: #fef3c7; border-left: 4px solid #f59e0b; border-radius: 6px;">
                                <p style="margin: 0; font-size: 14px; line-height: 1.6; color: #92400e;">
                                    <strong>⏰ Important:</strong> This verification link will expire in 24 hours.
                                </p>
                            </div>

                            <p style="margin: 20px 0 0 0; font-size: 14px; line-height: 1.6; color: #64748b;">
                                If you did not create an account with QR Dada, please ignore this email and no action is required.
                            </p>
                        </td>
                    </tr>

                    <!-- Footer -->
                    <tr>
                        <td style="padding: 30px 40px; background-color: #f8fafc; border-radius: 0 0 12px 12px; border-top: 1px solid #e2e8f0;">
                            <p style="margin: 0 0 10px 0; font-size: 14px; color: #64748b; text-align: center;">
                                Thanks,<br>
                                <strong style="color: #1e293b;">The QR Dada Team</strong>
                            </p>

                            <p style="margin: 20px 0 0 0; font-size: 12px; color: #94a3b8; text-align: center;">
                                © {datetime.now().year} QR Dada. All rights reserved.
                            </p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>
'''

        # Send the email using OAuth2 support email
        email_service.send_support_email(
            to=user.company_email,
            subject=subject,
            body=body,
            html=html
        )
        
        EmailLog.log_email(
            recipient_email=user.company_email,
            recipient_name=user.name,
            email_type='verification',
            subject=subject,
            user_id=user.id,
            status='sent',
            metadata={'token_generated': True, 'expires_24h': True}
        )
        
        return True
        
    except Exception as e:
        
        EmailLog.log_email(
            recipient_email=user.company_email,
            recipient_name=user.name,
            email_type='verification',
            subject=subject,
            user_id=user.id,
            status='failed',
            error_message=str(e),
            metadata={'token_generated': True, 'expires_24h': True}
        )
        
        current_app.logger.error(f"Failed to send verification email to {user.company_email}: {str(e)}")
        return False

def send_reset_email(user):
    """Send password reset email with logging"""
    try:
        token = user.get_reset_token()
        subject = 'Password Reset Request - QR Dada'
        reset_url = url_for('reset_token', token=token, _external=True)
        logo_url = "https://qrdada.com/static/images/qr.png"

        # Plain text version
        body = f'''Hi {user.name},

We received a request to reset your password for your QR Dada account.

To reset your password, please click the following link:
{reset_url}

This link will expire in 1 hour for security reasons.

If you didn't request a password reset, please ignore this email.

Thanks,
The QR Dada Support Team
'''

        # HTML version
        html = f'''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Password Reset Request - QR Dada</title>
</head>
<body style="margin: 0; padding: 0; font-family: 'Inter', 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f8fafc;">
    <table role="presentation" style="width: 100%; border-collapse: collapse; background-color: #f8fafc;">
        <tr>
            <td align="center" style="padding: 40px 20px;">
                <table role="presentation" style="width: 100%; max-width: 600px; border-collapse: collapse; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);">
                    <!-- Logo Header -->
                    <tr>
                        <td align="center" style="padding: 40px 40px 20px 40px; background: linear-gradient(-45deg, #0ea5e9, #8b5cf6); border-radius: 12px 12px 0 0;">
                            <img src="{logo_url}" alt="QR Dada Logo" style="width: 80px; height: auto; display: block; background-color: #ffffff; padding: 10px; border-radius: 8px;">
                        </td>
                    </tr>

                    <!-- Main Content -->
                    <tr>
                        <td style="padding: 40px;">
                            <h1 style="margin: 0 0 20px 0; font-size: 28px; font-weight: 700; color: #1e293b; text-align: center;">
                                Reset Your Password
                            </h1>

                            <p style="margin: 0 0 20px 0; font-size: 16px; line-height: 1.6; color: #475569;">
                                Hi <strong>{user.name}</strong>,
                            </p>

                            <p style="margin: 0 0 20px 0; font-size: 16px; line-height: 1.6; color: #475569;">
                                We received a request to reset your password for your QR Dada account. Click the button below to create a new password.
                            </p>

                            <!-- CTA Button -->
                            <table role="presentation" style="width: 100%; border-collapse: collapse; margin: 30px 0;">
                                <tr>
                                    <td align="center">
                                        <a href="{reset_url}" style="display: inline-block; padding: 16px 40px; background: linear-gradient(-45deg, #0ea5e9, #8b5cf6); color: #ffffff; text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 16px; box-shadow: 0 4px 6px rgba(124, 58, 237, 0.3);">
                                            Reset Password
                                        </a>
                                    </td>
                                </tr>
                            </table>

                            <p style="margin: 20px 0; font-size: 14px; line-height: 1.6; color: #64748b; text-align: center;">
                                Or copy and paste this link into your browser:
                            </p>

                            <p style="margin: 0 0 20px 0; font-size: 13px; line-height: 1.6; color: #8b5cf6; word-break: break-all; text-align: center; background-color: #f1f5f9; padding: 12px; border-radius: 6px;">
                                {reset_url}
                            </p>

                            <div style="margin: 30px 0; padding: 20px; background-color: #fee2e2; border-left: 4px solid #ef4444; border-radius: 6px;">
                                <p style="margin: 0; font-size: 14px; line-height: 1.6; color: #991b1b;">
                                    <strong>🔒 Security Notice:</strong> If you didn't request a password reset, please ignore this email. Your password will remain unchanged.
                                </p>
                            </div>

                            <p style="margin: 20px 0 0 0; font-size: 14px; line-height: 1.6; color: #64748b;">
                                This link will expire in 1 hour for security reasons.
                            </p>
                        </td>
                    </tr>

                    <!-- Footer -->
                    <tr>
                        <td style="padding: 30px 40px; background-color: #f8fafc; border-radius: 0 0 12px 12px; border-top: 1px solid #e2e8f0;">
                            <p style="margin: 0 0 10px 0; font-size: 14px; color: #64748b; text-align: center;">
                                Thanks,<br>
                                <strong style="color: #1e293b;">The QR Dada Support Team</strong>
                            </p>

                            <p style="margin: 20px 0 0 0; font-size: 12px; color: #94a3b8; text-align: center;">
                                © {datetime.now().year} QR Dada. All rights reserved.
                            </p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>
'''

        # Send the email using OAuth2 support email
        email_service.send_support_email(
            to=user.company_email,
            subject=subject,
            body=body,
            html=html
        )

        EmailLog.log_email(
            recipient_email=user.company_email,
            recipient_name=user.name,
            email_type='password_reset',
            subject=subject,
            user_id=user.id,
            status='sent',
            metadata={'token_generated': True, 'reset_request': True}
        )
        
        return True
        
    except Exception as e:
        
       
        EmailLog.log_email(
            recipient_email=user.company_email,
            recipient_name=user.name,
            email_type='password_reset',
            subject=subject,
            user_id=user.id,
            status='failed',
            error_message=str(e),
            metadata={'token_generated': True, 'reset_request': True}
        )
        
        current_app.logger.error(f"Failed to send reset email to {user.company_email}: {str(e)}")
        return False

application = create_app()
# Add this after your app creation and before routes
# @app.teardown_appcontext
# def close_db(error):
#     """Close database connections properly"""
#     if error:
#         db.session.rollback()
#     db.session.remove()

# @app.errorhandler(Exception)
# def handle_database_errors(error):
#     """Handle database errors and rollback transactions"""
#     if 'PendingRollbackError' in str(error) or 'InvalidTransaction' in str(error):
#         try:
#             db.session.rollback()
#         except:
#             pass
#         try:
#             db.session.remove()
#         except:
#             pass
    
#     app.logger.error(f"Database error: {str(error)}")
#     db.session.rollback()
#     return "Database error occurred", 500

@app.context_processor
def inject_website_settings():
    """Make website settings available to all templates"""
    try:
        # Get settings with robust defaults
        website_name = WebsiteSettings.get_setting('website_name', 'QR Dada')
        website_icon = WebsiteSettings.get_setting('website_icon', 'fas fa-qrcode')
        website_logo_file = WebsiteSettings.get_setting('website_logo_file')
        website_tagline = WebsiteSettings.get_setting('website_tagline', 'Professional QR Code Analytics Platform')
        
        # Ensure website_name is never None or empty
        if not website_name or not website_name.strip():
            website_name = 'QR Dada'
            
        # Ensure website_icon is never None or empty
        if not website_icon or not website_icon.strip():
            website_icon = 'fas fa-qrcode'
            
        # Ensure website_tagline is never None or empty
        if not website_tagline or not website_tagline.strip():
            website_tagline = 'Professional QR Code and Analytics Platform'
        
        website_settings = {
            'website_name': website_name,
            'website_icon': website_icon,
            'website_logo_file': website_logo_file,
            'website_tagline': website_tagline
        }
        
        return dict(website_settings=website_settings, current_year=datetime.now().year)

    except Exception as e:
        # Comprehensive fallback to defaults if database is not available
        app.logger.error(f"Error loading website settings: {str(e)}")
        return dict(website_settings={
            'website_name': 'QR Dada',
            'website_icon': 'fas fa-qrcode',
            'website_logo_file': None,
            'website_tagline': 'Professional QR Code and Analytics Platform'
        }, current_year=datetime.now().year)

# ---------------------------------------
# user login signup and reset password
# ---------------------------------------

@app.route('/')
def index():
        
        return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    # Check if user is already logged in
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        # Get form data
        company_email = request.form.get('companyEmail', '').strip()
        password = request.form.get('password', '').strip()
        
        # Input validation - Check if both fields are provided
        if not company_email or not password:
            flash('Email and password are required.', 'danger')
            return render_template('login.html', email_value=request.form.get('companyEmail', ''))
        
        # Normalize email input to lowercase
        company_email = company_email.lower()
        
        # Validate user using SQLAlchemy with case-insensitive search
        user = User.query.filter(
            func.lower(User.company_email) == company_email
        ).first()
        
        # Check if user exists
        if not user:
            flash('Invalid email or password.', 'danger')
            return render_template('login.html', email_value=request.form.get('companyEmail', ''))

        # Check if email is confirmed
        if not user.email_confirmed:
            flash('Please verify your email before logging in. Check your inbox or request a new verification link.', 'warning')
            return redirect(url_for('resend_verification'))
        
        # Check if user has password set (similar to admin check)
        if not hasattr(user, 'password_hash') or not user.password_hash:
            flash('Password not set for this account. Please contact administrator.', 'danger')
            return render_template('login.html', email_value=request.form.get('companyEmail', ''))
        
        # Verify password with error handling
        try:
            if user.check_password(password):
                # Successful login
                login_user(user)  # Using Flask-Login
                session['user_id'] = user.id
                session['user_name'] = user.name
                session['email_id'] = user.company_email
                
                # Store additional user data if available
                if hasattr(user, 'role'):
                    session['user_role'] = user.role
                if hasattr(user, 'permissions'):
                    session['user_permissions'] = user.permissions if isinstance(user.permissions, list) else []
                
                flash('Login successful!', 'success')
                
                # Redirect to next page if specified, otherwise dashboard
                next_page = request.args.get('next')
                if next_page and urlparse(next_page).netloc == '':
                    return redirect(next_page)
                return redirect(url_for('dashboard'))
            else:
                # Invalid password
                flash('Invalid email or password.', 'danger')
                return render_template('login.html', email_value=request.form.get('companyEmail', ''))
                
        except Exception as e:
            # Log the error for debugging
            app.logger.error(f"Password verification error for user {company_email}: {str(e)}")
            flash('Error verifying password. Please contact administrator.', 'danger')
            return render_template('login.html', email_value=request.form.get('companyEmail', ''))

    # GET request - show login form
    return render_template('login.html', email_value='')

@app.route('/register', methods=['GET', 'POST'])
def register():
    # Check if user is already logged in
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return signup()  

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    # Check if user is already logged in
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        company_email = request.form.get('companyEmail', '').lower().strip()  # Normalize email immediately
        password = request.form.get('password')
        retype_password = request.form.get('retypePassword')
        
        # Enhanced input validation
        errors = []
        
        # Name validation
        if not name or len(name.strip()) < 2:
            errors.append("Name should be at least 2 characters long.")
        
        # Email validation
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not company_email or not re.match(email_pattern, company_email):
            errors.append("Please enter a valid email address.")
        
        # Password validation
        if not password:
            errors.append("Password is required.")
        elif len(password) < 8:
            errors.append("Password must be at least 8 characters long.")
        elif not re.search(r'[A-Z]', password):
            errors.append("Password must contain at least one uppercase letter.")
        elif not re.search(r'[a-z]', password):
            errors.append("Password must contain at least one lowercase letter.")
        elif not re.search(r'[0-9]', password):
            errors.append("Password must contain at least one number.")
        elif not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
            errors.append("Password must contain at least one special character.")
        
        # Password confirmation validation
        if password != retype_password:
            errors.append("Passwords do not match.")
        
        # Check if email already exists (case-insensitive)
        existing_user = User.query.filter(
            func.lower(User.company_email) == company_email
        ).first()
        if existing_user:
            errors.append("This email is already registered.")
        
        # If there are any errors, flash them and redirect back to signup
        if errors:
            for error in errors:
                flash(error, "danger")
            # Return original email format for display, but errors will show
            return render_template('signup.html', name=name, company_email=request.form.get('companyEmail', ''))
        
        # Create new user with email verification required
        # Email will be automatically normalized to lowercase in User.init
        new_user = User(name=name, company_email=company_email, email_confirmed=False)
        new_user.set_password(password)
        db.session.add(new_user)
        db.session.commit()
            
        # Send verification email
        try:
            send_verification_email(new_user)
            flash("Signup successful! Please check your email to verify your account.", "success")
        except Exception as e:
            logging.error(f"Error sending verification email: {str(e)}")
            flash("Signup successful but there was an issue sending the verification email. Please contact support.", "warning")
        
        return redirect(url_for('verify_account', email=company_email))
    
    return render_template('signup.html')

@app.route('/check-email-availability', methods=['POST'])
def check_email_availability():
    """
    AJAX endpoint to check if an email address is already registered
    """
    try:
        # Get email from request
        data = request.get_json()
        if not data or 'email' not in data:
            return jsonify({'error': 'Email is required'}), 400
        
        email = data['email'].lower().strip()
        
        # Validate email format
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(email_pattern, email):
            return jsonify({'error': 'Invalid email format'}), 400
        
        # Check if email exists in database
        existing_user = User.query.filter(
            func.lower(User.company_email) == email
        ).first()
        
        if existing_user:
            # Email is already registered
            return jsonify({
                'available': False,
                'message': 'Email is already registered'
            })
        else:
            # Email is available
            return jsonify({
                'available': True,
                'message': 'Email is available'
            })
            
    except Exception as e:
        app.logger.error(f"Error checking email availability: {str(e)}")
        return jsonify({'error': 'Server error occurred'}), 500
    
@app.route('/check_password', methods=['POST'])
def check_password():
    data = request.get_json()
    password = data.get('password')
    
    # Compare with hashed password in DB
    if check_password_hash(current_user.password_hash, password):
        return jsonify({'valid': True})
    else:
        return jsonify({'valid': False})

@app.route("/verify_account")
def verify_account():
    email = request.args.get('email')
    return render_template('verify_account.html', email=email)

@app.route('/verify_email/<token>')
def verify_email(token):
    user = User.verify_email_token(token)
    if user is None:
        flash('Invalid or expired verification link. Please request a new one.', 'danger')
        return redirect(url_for('resend_verification'))
    
    user.email_confirmed = True
    user.email_confirm_token = None
    user.email_token_created_at = None
    db.session.commit()
    
    flash('Your email has been verified! You can now log in.', 'success')
    return redirect(url_for('login'))

@app.route('/resend_verification', methods=['GET', 'POST'])
def resend_verification():
    if request.method == 'POST':
        # Normalize email for consistent lookup
        email = request.form.get('companyEmail', '').lower().strip()
        user = User.query.filter(
            func.lower(User.company_email) == email
        ).first()
        
        if user and not user.email_confirmed:
            try:
                send_verification_email(user)
                flash('A new verification email has been sent.', 'success')
            except Exception as e:
                logging.error(f"Error resending verification email: {str(e)}")
                flash('There was an issue sending the verification email. Please try again later.', 'danger')
        elif user and user.email_confirmed:
            flash('This email is already verified. You can log in.', 'info')
        else:
            flash('Email not found. Please sign up first.', 'warning')
            
        return redirect(url_for('login'))
    
    return render_template('resend_verification.html')

@app.route('/reset_password', methods=['GET', 'POST'])
def reset_request():
    if request.method == 'POST':
        # Normalize email for consistent lookup
        email = request.form.get('companyEmail', '').lower().strip()
        user = User.query.filter(
            func.lower(User.company_email) == email
        ).first()
        if user:
            send_reset_email(user)
            flash('An email has been sent with instructions to reset your password.', 'info')
            return redirect(url_for('login'))
        else:
            flash('Email not found. Please register first.', 'warning')
    return render_template('reset_request.html')

@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_token(token):
    try:
        # Try to verify the token
        user = User.verify_reset_token(token)
        if not user:
            flash('Invalid or expired token. Please request a new password reset link.', 'danger')
            return redirect(url_for('reset_request'))

        if request.method == 'POST':
            # Handle password reset logic here
            password = request.form.get('password')
            confirm_password = request.form.get('confirm_password')
            
            # Validate passwords
            if not password or not confirm_password:
                flash('Both password fields are required', 'danger')
                return render_template('reset_token.html', token=token)
            
            if password != confirm_password:
                flash('Passwords do not match', 'danger')
                return render_template('reset_token.html', token=token)
            
            if len(password) < 8:
                flash('Password must be at least 8 characters long', 'danger')
                return render_template('reset_token.html', token=token)

            # Update password
            user.set_password(password)
            user.password_reset_at = datetime.now(UTC)
            db.session.commit()

            flash('Your password has been updated! You can now log in with your new password.', 'success')
            return redirect(url_for('login'))

    except Exception as e:
        # Log any errors
        logging.error(f"Error during password reset: {str(e)}")
        flash('An error occurred during the password reset process. Please try again.', 'danger')
        return redirect(url_for('reset_request'))

    # If method is GET, render the reset password page
    return render_template('reset_token.html', token=token)

@app.route('/logout')
@login_required
def logout():
    logout_user()  # Flask-Login function
    session.clear()
    flash("Logged out successfully.", "success")
    return redirect(url_for('index'))

# ---------------------------------------
# Profile Management Routes
# ---------------------------------------

# Add these helper functions at the top of your routes file

def get_dynamic_qr_codes(user_id):
    """
    Get all dynamic QR codes for a user.
    Returns list of dynamic QR codes.
    """
    qr_codes = QRCode.query.filter_by(user_id=user_id).all()
    dynamic_qr_codes = []
    
    for qr_code in qr_codes:
        # Option 1: If you have an is_dynamic field
        if hasattr(qr_code, 'is_dynamic') and qr_code.is_dynamic:
            dynamic_qr_codes.append(qr_code)
        
        # Option 2: If dynamic QR codes have a specific type (uncomment if needed)
        # elif qr_code.qr_type == 'dynamic':
        #     dynamic_qr_codes.append(qr_code)
        
        # Option 3: If you have a different field name (update accordingly)
        # elif hasattr(qr_code, 'dynamic') and qr_code.dynamic:
        #     dynamic_qr_codes.append(qr_code)
    
    return dynamic_qr_codes

def calculate_total_analytics_operations(user_id):
    """
    Calculate total analytics operations (scans of dynamic QR codes) for a user.
    This matches the logic used in user_analytics route.
    """
    dynamic_qr_codes = get_dynamic_qr_codes(user_id)
    
    total_scans = 0
    for qr_code in dynamic_qr_codes:
        scans = Scan.query.filter_by(qr_code_id=qr_code.id).all()
        total_scans += len(scans)
    
    return total_scans

def calculate_analytics_usage_data(user_id, subscription):
    """
    Calculate comprehensive analytics usage data.
    Returns dictionary with all analytics metrics.
    """
    if not subscription or not subscription.effective_analytics_limit:
        return {
            'analytics_used': 0,
            'analytics_remaining': 0,
            'analytics_total': 0,
            'analytics_percent': 0,
            'dynamic_qr_count': 0
        }

    # Get total analytics operations using same logic as user_analytics
    total_analytics_operations = calculate_total_analytics_operations(user_id)
    dynamic_qr_count = len(get_dynamic_qr_codes(user_id))

    analytics_total = subscription.effective_analytics_limit
    analytics_remaining = max(0, analytics_total - total_analytics_operations)
    analytics_percent = min(100, (total_analytics_operations / analytics_total) * 100) if analytics_total > 0 else 0
    
    return {
        'analytics_used': total_analytics_operations,
        'analytics_remaining': analytics_remaining,
        'analytics_total': analytics_total,
        'analytics_percent': analytics_percent,
        'dynamic_qr_count': dynamic_qr_count
    }

def calculate_todays_analytics_operations(user_id):
    """
    Calculate today's analytics operations (scans of dynamic QR codes today).
    """
    dynamic_qr_codes = get_dynamic_qr_codes(user_id)
    
    # Get today's date range
    today = datetime.now(UTC).date()
    today_start = datetime.combine(today, datetime.min.time()).replace(tzinfo=UTC)
    today_end = datetime.combine(today, datetime.max.time()).replace(tzinfo=UTC)
    
    todays_operations = 0
    for qr_code in dynamic_qr_codes:
        scans_today = Scan.query.filter_by(qr_code_id=qr_code.id).filter(
            Scan.timestamp >= today_start,
            Scan.timestamp <= today_end
        ).count()
        todays_operations += scans_today
    
    return todays_operations

# FIXED: Dashboard Route - Count QR codes for current subscription period only

@app.route('/dashboard')
@login_required
def dashboard():
    user_id = current_user.id
    
    # Get user's QR codes
    qr_codes = QRCode.query.filter_by(user_id=user_id).all()
    
    # Get active subscription data
    active_subscription = (
        SubscribedUser.query
        .filter(SubscribedUser.U_ID == user_id)
        .filter(SubscribedUser.end_date > datetime.now(UTC))
        .filter(SubscribedUser._is_active == True)
        .join(Subscription, SubscribedUser.S_ID == Subscription.S_ID)
        .first()
    )
    
    # FIXED: Use subscription's actual analytics values instead of calculate_analytics_usage_data
    if active_subscription:
        # Get the actual analytics data from subscription object (matches subscription.html)
        analytics_data = {
            'analytics_used': active_subscription.analytics_used,
            'analytics_remaining': max(0, active_subscription.effective_analytics_limit - active_subscription.analytics_used),
            'analytics_total': active_subscription.effective_analytics_limit,
            'analytics_percent': active_subscription.analytics_percent
        }
    else:
        analytics_data = {
            'analytics_used': 0,
            'analytics_remaining': 0,
            'analytics_total': 0,
            'analytics_percent': 0
        }

    # Prepare subscription data for template
    subscription_data = {
        'has_subscription': False,
        'plan_name': 'No Subscription',
        'days_remaining': 0,
        'qr_remaining': 0,
        'qr_total': 0,
        'qr_used': 0,
        'qr_percent': 0,
        'expires_on': None,
        'is_auto_renew': False,
        'subscription_id': None,
        # Analytics data - use actual subscription values
        'analytics_used': analytics_data['analytics_used'],
        'analytics_remaining': analytics_data['analytics_remaining'],
        'analytics_total': analytics_data['analytics_total'],
        'analytics_percent': analytics_data['analytics_percent']
    }
    
    if active_subscription:
        qr_total = active_subscription.effective_qr_limit
        
        # FIXED: Count QR codes created ONLY during current subscription period
        subscription_start_date = active_subscription.start_date
        subscription_end_date = active_subscription.end_date
        
        # Count QR codes created within subscription period
        qr_used_in_period = QRCode.query.filter(
            QRCode.user_id == user_id,
            QRCode.created_at >= subscription_start_date,
            QRCode.created_at <= subscription_end_date
        ).count()
        
        print(f"Subscription period: {subscription_start_date} to {subscription_end_date}")
        print(f"QR codes in this subscription period: {qr_used_in_period}")
        
        # Use subscription period count
        qr_used = qr_used_in_period
        
        # SYNC: Update subscription table with period-specific count
        if active_subscription.qr_generated != qr_used_in_period:
            print(f"Updating subscription qr_generated from {active_subscription.qr_generated} to {qr_used_in_period}")
            
            try:
                active_subscription.qr_generated = qr_used_in_period
                db.session.commit()
                print(f"Successfully synced subscription QR count to {qr_used_in_period}")
            except Exception as e:
                print(f"Error syncing QR count: {e}")
                db.session.rollback()
        
        # Calculate remaining and percentage based on subscription period
        qr_remaining = max(0, qr_total - qr_used)
        qr_percent = (qr_used / qr_total) * 100 if qr_total > 0 else 0
        
        subscription_data.update({
            'has_subscription': True,
            'plan_name': active_subscription.subscription.plan,
            'days_remaining': active_subscription.days_remaining,
            'qr_remaining': qr_remaining,
            'qr_total': qr_total,
            'qr_used': qr_used,  # Only QR codes from current subscription period
            'qr_percent': qr_percent,
            'expires_on': active_subscription.end_date,
            'is_auto_renew': active_subscription.is_auto_renew,
            'subscription_id': active_subscription.id
        })
        
        # Debug output
        print(f"Dashboard QR counts (subscription period only) - Used: {qr_used}, Remaining: {qr_remaining}, Total: {qr_total}")
    
    # Count total QR codes (for general stats)
    total_qr_codes = len(qr_codes)
    
    today = datetime.now(UTC).date()
    today_start = datetime.combine(today, datetime.min.time()).replace(tzinfo=UTC)
    today_end = datetime.combine(today, datetime.max.time()).replace(tzinfo=UTC)
    
    qr_created_today = QRCode.query.filter_by(user_id=user_id).filter(
        QRCode.created_at >= today_start,
        QRCode.created_at <= today_end
    ).count()
    
    return render_template('dashboard.html',
                          qr_codes=qr_codes,
                          **subscription_data,
                          total_qr_codes=total_qr_codes,
                          qr_created_today=qr_created_today)


# ALSO UPDATE: Profile route to use same logic

@app.route('/profile')
@login_required
def profile():
    user_id = session.get('user_id')
    
    # FIXED: Add proper user validation
    if not user_id:
        app.logger.error("No user_id found in session")
        flash('Session expired. Please log in again.', 'warning')
        return redirect(url_for('login'))
    
    user = User.query.get(user_id)
    
    # FIXED: Handle case where user doesn't exist
    if not user:
        app.logger.error(f"User with ID {user_id} not found in database")
        # Clear invalid session data
        session.clear()
        flash('User not found. Please log in again.', 'warning')
        return redirect(url_for('login'))
    
    # Get user's active subscription
    subscription = (
        db.session.query(SubscribedUser)
        .filter(SubscribedUser.U_ID == user_id)
        .filter(SubscribedUser.end_date > datetime.now(UTC))
        .filter(SubscribedUser._is_active == True)
        .join(Subscription, SubscribedUser.S_ID == Subscription.S_ID)
        .first()
    )
    
    # FIXED: Use subscription's actual analytics values instead of calculate_analytics_usage_data
    analytics_data = None
    if subscription:
        # Get the actual analytics data from subscription object (matches subscription.html)
        analytics_data = {
            'analytics_used': subscription.analytics_used,
            'analytics_remaining': max(0, subscription.effective_analytics_limit - subscription.analytics_used),
            'analytics_total': subscription.effective_analytics_limit,
            'analytics_percent': subscription.analytics_percent,
            'dynamic_qr_count': 0  # Not needed for profile page
        }

        # Update analytics with subscription-period QR count
        subscription_start_date = subscription.start_date
        subscription_end_date = subscription.end_date

        qr_count_in_period = QRCode.query.filter(
            QRCode.user_id == user_id,
            QRCode.created_at >= subscription_start_date,
            QRCode.created_at <= subscription_end_date
        ).count()

        print(f"Profile: QR codes in subscription period: {qr_count_in_period}")

        # Update subscription's qr_generated to match period count
        if subscription.qr_generated != qr_count_in_period:
            subscription.qr_generated = qr_count_in_period
            try:
                db.session.commit()
            except Exception as e:
                app.logger.error(f"Error updating subscription QR count: {e}")
                db.session.rollback()
    
    # Get recent payments - Show completed and pending payments
    payments = []
    try:
        payments = (
            Payment.query
            .filter_by(user_id=user_id)
            .filter(Payment.status.in_(['completed', 'pending']))  # Show completed and pending payments
            .order_by(Payment.created_at.desc())
            .limit(10)
            .all()
        )
    except Exception as e:
        app.logger.error(f"Error fetching payments for user {user_id}: {e}")
    
    # Count total QR codes for the user (all time)
    total_qr_count = 0
    try:
        total_qr_count = QRCode.query.filter_by(user_id=user_id).count()
    except Exception as e:
        app.logger.error(f"Error counting QR codes for user {user_id}: {e}")
    
    # Calculate QR codes created today
    today = datetime.now(UTC).date()
    today_start = datetime.combine(today, datetime.min.time()).replace(tzinfo=UTC)
    today_end = datetime.combine(today, datetime.max.time()).replace(tzinfo=UTC)
    
    qr_created_today = 0
    try:
        qr_created_today = QRCode.query.filter_by(user_id=user_id).filter(
            QRCode.created_at >= today_start,
            QRCode.created_at <= today_end
        ).count()
    except Exception as e:
        app.logger.error(f"Error counting today's QR codes for user {user_id}: {e}")
    
    # Calculate today's analytics operations
    analytics_operations_today = 0
    try:
        analytics_operations_today = calculate_todays_analytics_operations(user_id)
    except Exception as e:
        app.logger.error(f"Error calculating today's analytics for user {user_id}: {e}")
    
    # FIXED: Ensure all required data is available before rendering
    return render_template(
        'profile.html',
        user=user,
        subscription=subscription,
        analytics_data=analytics_data,
        payments=payments,
        total_qr_count=total_qr_count,
        qr_created_today=qr_created_today,
        scans_today=analytics_operations_today
    )
@app.route('/update_profile', methods=['POST'])
@login_required
def update_profile():
    user_id = session.get('user_id')
    user = User.query.get(user_id)
    
    if not user:
        flash('User not found', 'danger')
        return redirect(url_for('profile'))
    
    update_type = request.form.get('update_type', 'account')
    
    if update_type == 'account':
        # Update name
        name = request.form.get('name')
        if name and name.strip():
            user.name = name.strip()
            session['user_name'] = name.strip()  # Update session data too
            
        db.session.commit()
        flash('Profile information updated successfully', 'success')
        return redirect(url_for('profile') + '#account')
        
    elif update_type == 'security':
        # Process password change
        current_password = request.form.get('currentPassword')
        new_password = request.form.get('newPassword')
        confirm_password = request.form.get('confirmPassword')
        
        # Validate input fields
        if not all([current_password, new_password, confirm_password]):
            flash('All password fields are required', 'danger')
            return redirect(url_for('profile') + '#security')
        
        # Verify current password
        if not user.check_password(current_password):
            flash('Current password is incorrect', 'danger')
            return redirect(url_for('profile') + '#security')
        
        # Validate new password
        if new_password != confirm_password:
            flash('New passwords do not match', 'danger')
            return redirect(url_for('profile') + '#security')
        
        # Password complexity validation
        password_errors = []
        if len(new_password) < 8:
            password_errors.append('Password must be at least 8 characters long')
        if not re.search(r'[A-Z]', new_password):
            password_errors.append('Password must contain at least one uppercase letter')
        if not re.search(r'[a-z]', new_password):
            password_errors.append('Password must contain at least one lowercase letter')
        if not re.search(r'[0-9]', new_password):
            password_errors.append('Password must contain at least one number')
        if not re.search(r'[!@#$%^&*(),.?\":{}|<>]', new_password):
            password_errors.append('Password must contain at least one special character')
        
        if password_errors:
            for error in password_errors:
                flash(error, 'danger')
            return redirect(url_for('profile') + '#security')
        
        # Check if new password is different from current
        if user.check_password(new_password):
            flash('New password must be different from current password', 'warning')
            return redirect(url_for('profile') + '#security')
        
        # Update password
        user.set_password(new_password)
        db.session.commit()
        
        # Log the password change (optional)
        logging.info(f"Password changed for user ID {user_id}")
        
        flash('Password updated successfully. Please use your new password next time you log in.', 'success')
        return redirect(url_for('profile') + '#security')
    
    # If we get here, something went wrong
    flash('Invalid update request', 'danger')
    return redirect(url_for('profile'))

# Generate a downloadable payment receipt
@app.route('/receipt/<payment_id>')
@login_required
def download_receipt(payment_id):
    user_id = session.get('user_id')
    
    # Get payment details
    payment = Payment.query.filter_by(id=payment_id, user_id=user_id).first_or_404()
    
    # TODO: Generate and return PDF receipt
    # This would typically use a PDF generation library like ReportLab or WeasyPrint
    
    flash('Receipt download feature coming soon!', 'info')
    return redirect(url_for('profile') + '#activity')

# OPTIONAL: Add a helper function to calculate analytics usage consistently
def calculate_analytics_usage(user_id, subscription=None):
    """
    Calculate analytics usage based on scans of dynamic QR codes.
    Returns a dictionary with analytics usage data.
    """
    if not subscription or not subscription.effective_analytics_limit:
        return {
            'analytics_used': 0,
            'analytics_remaining': 0,
            'analytics_total': 0,
            'analytics_percent': 0
        }
    
    analytics_total = subscription.effective_analytics_limit
    
    # Get all dynamic QR codes for the user
    qr_codes = QRCode.query.filter_by(user_id=user_id).all()
    dynamic_qr_codes = [qr for qr in qr_codes if hasattr(qr, 'is_dynamic') and qr.is_dynamic]
    
    # Count all scans of dynamic QR codes
    total_scans = 0
    for qr_code in dynamic_qr_codes:
        scan_count = Scan.query.filter_by(qr_code_id=qr_code.id).count()
        total_scans += scan_count
    
    analytics_used = total_scans
    analytics_remaining = max(0, analytics_total - analytics_used)
    analytics_percent = min(100, (analytics_used / analytics_total) * 100) if analytics_total > 0 else 0
    
    return {
        'analytics_used': analytics_used,
        'analytics_remaining': analytics_remaining,
        'analytics_total': analytics_total,
        'analytics_percent': analytics_percent
    }
@app.route('/create', methods=['GET', 'POST'])
@login_required
def create_qr():
    """Complete create QR route with enhanced logo handling and debugging"""
    user_id = session.get('user_id')
    
    # Check if user has an active subscription
    active_subscription = (
        SubscribedUser.query
        .filter(SubscribedUser.U_ID == user_id)
        .filter(SubscribedUser.end_date > datetime.now(UTC))
        .filter(SubscribedUser._is_active == True)
        .first()
    )
    
    if not active_subscription:
        flash('You need an active subscription to create QR codes.', 'warning')
        return redirect(url_for('subscription.user_subscriptions'))
    
    # Check if user has reached QR generation limit
    if active_subscription.qr_generated >= active_subscription.effective_qr_limit:
        flash('You have reached your QR code generation limit for this subscription plan.', 'warning')
        return redirect(url_for('subscription.user_subscriptions'))
    
    if request.method == 'POST':
        try:
            print("=== CREATE QR CODE START ===")
            print(f"User ID: {user_id}")
            print(f"Files in request: {list(request.files.keys())}")
            print(f"Form data keys: {list(request.form.keys())}")
            
            # Extract basic QR code information with validation
            qr_type = request.form.get('qr_type')
            name = request.form.get('name', '').strip()
            is_dynamic = 'is_dynamic' in request.form and request.form.get('is_dynamic') == 'true'
            
            print(f"QR Type: {qr_type}, Name: {name}, Dynamic: {is_dynamic}")
            
            # FIXED: Enhanced dynamic QR check - allow dynamic QR for dynamic plans
            if is_dynamic:
                subscription_plan = active_subscription.subscription

                # Check if plan allows dynamic QR codes
                # Allow dynamic QR codes if:
                # 1. Plan type is specifically "Dynamic" (case insensitive), OR
                # 2. Plan type is not "Normal" and tier >= 2, OR
                # 3. Plan name contains "dynamic" (case insensitive)
                plan_type_lower = subscription_plan.plan_type.lower()
                plan_name_lower = subscription_plan.plan.lower()

                can_create_dynamic_qr = (
                    plan_type_lower == 'dynamic' or
                    'dynamic' in plan_name_lower or
                    (plan_type_lower != 'normal' and subscription_plan.tier >= 2)
                )

                if not can_create_dynamic_qr:
                    flash('Your subscription plan does not include dynamic QR codes. Please upgrade to a plan that supports dynamic QR codes.', 'warning')
                    return redirect(url_for('create_qr'))
            
            # Check if selected design is allowed for user's subscription
            selected_template = request.form.get('template', '')
            if selected_template and not active_subscription.is_design_allowed(selected_template):
                flash(f'Your subscription plan does not include access to the {selected_template} design.', 'warning')
                return redirect(url_for('create_qr'))
            
            # Validate required fields
            if not name or not qr_type:
                missing = []
                if not name:
                    missing.append("QR code name")
                if not qr_type:
                    missing.append("QR type")
                flash(f'Required fields missing: {", ".join(missing)}', 'error')
                return redirect(url_for('create_qr'))
            
            # Create content JSON with actual data based on QR type
            content = {}
            
            # Populate content based on QR type
            if qr_type == 'link':
                url = request.form.get('url', '').strip()
                if not url:
                    flash('URL is required for link QR codes.', 'error')
                    return redirect(url_for('create_qr'))
                content['url'] = url
                print(f"Link QR - URL: {url}")
                
            elif qr_type == 'email':
                email = request.form.get('email', '').strip()
                if not email:
                    flash('Email address is required for email QR codes.', 'error')
                    return redirect(url_for('create_qr'))
                content['email'] = email
                content['subject'] = request.form.get('subject', '')
                content['body'] = request.form.get('body', '')
                print(f"Email QR - Email: {email}")
                
            elif qr_type == 'text':
                text = request.form.get('text', '').strip()
                if not text:
                    flash('Text content is required for text QR codes.', 'error')
                    return redirect(url_for('create_qr'))
                content['text'] = text
                print(f"Text QR - Text length: {len(text)}")
                
            elif qr_type == 'call':
                phone = request.form.get('phone', '').strip()
                if not phone:
                    flash('Phone number is required for call QR codes.', 'error')
                    return redirect(url_for('create_qr'))
                content['phone'] = phone
                print(f"Call QR - Phone: {phone}")
                
            elif qr_type == 'sms':
                phone = request.form.get('sms-phone', '').strip() or request.form.get('phone', '').strip()
                if not phone:
                    flash('Phone number is required for SMS QR codes.', 'error')
                    return redirect(url_for('create_qr'))
                content['phone'] = phone
                content['message'] = request.form.get('message', '')
                print(f"SMS QR - Phone: {phone}")
                
            elif qr_type == 'whatsapp':
                phone = request.form.get('whatsapp-phone', '').strip() or request.form.get('phone', '').strip()
                if not phone:
                    flash('Phone number is required for WhatsApp QR codes.', 'error')
                    return redirect(url_for('create_qr'))
                content['phone'] = phone
                content['message'] = request.form.get('whatsapp-message', '') or request.form.get('message', '')
                print(f"WhatsApp QR - Phone: {phone}")
                
            elif qr_type == 'wifi':
                ssid = request.form.get('ssid', '').strip()
                if not ssid:
                    flash('Network name (SSID) is required for WiFi QR codes.', 'error')
                    return redirect(url_for('create_qr'))
                content['ssid'] = ssid
                content['password'] = request.form.get('password', '')
                content['encryption'] = request.form.get('encryption', 'WPA')
                content['hidden_network'] = 'hidden_network' in request.form
                print(f"WiFi QR - SSID: {ssid}")
                
            elif qr_type == 'vcard':
                full_name = request.form.get('full_name', '').strip()
                if not full_name:
                    flash('Full name is required for vCard QR codes.', 'error')
                    return redirect(url_for('create_qr'))
                
                # Basic vCard information
                content['name'] = full_name
                content['phone'] = request.form.get('vcard-phone', '') or request.form.get('phone', '')
                content['email'] = request.form.get('vcard-email', '') or request.form.get('email', '')
                content['company'] = request.form.get('company', '')
                content['title'] = request.form.get('title', '')
                content['address'] = request.form.get('address', '')
                content['website'] = request.form.get('website', '')
                
                # Enhanced vCard information
                content['primary_color'] = request.form.get('vcard_primary_color', '#2c5282')
                content['secondary_color'] = request.form.get('vcard_secondary_color', '#3182ce')
                
                # Process social media links
                social_media = {}
                social_platforms = ['facebook', 'twitter', 'linkedin', 'instagram', 
                                   'youtube', 'whatsapp', 'telegram', 'github', 
                                   'tiktok', 'pinterest', 'snapchat', 'discord', 'reddit', 'tumblr']
                
                for platform in social_platforms:
                    social_url = request.form.get(f'social_{platform}', '').strip()
                    if social_url:
                        social_media[platform] = social_url
                
                if social_media:
                    content['social_media'] = social_media
                
                print(f"vCard QR - Name: {full_name}")
                
            elif qr_type == 'event':
                event_title = request.form.get('event-title', '').strip() or request.form.get('title', '').strip()
                if not event_title:
                    flash('Event title is required for event QR codes.', 'error')
                    return redirect(url_for('create_qr'))

                content['title'] = event_title
                content['location'] = request.form.get('location', '')
                content['description'] = request.form.get('description', '')
                content['organizer'] = request.form.get('organizer', '')

                # Handle datetime fields carefully
                content['start_date'] = request.form.get('start_date', '')
                content['end_time'] = request.form.get('end_time', '')
                print(f"Event QR - Title: {event_title}")
                print(f"Event QR - Description from form: '{content['description']}'")

            elif qr_type == 'image':
                if 'qr_image_file' not in request.files or not request.files['qr_image_file'].filename:
                    flash('Please upload an image for image QR codes.', 'error')
                    return redirect(url_for('create_qr'))

                image_file = request.files['qr_image_file']
                allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
                file_ext = image_file.filename.rsplit('.', 1)[-1].lower() if '.' in image_file.filename else ''
                if file_ext not in allowed_extensions:
                    flash('Invalid image format. Supported: JPG, PNG, GIF, WebP', 'error')
                    return redirect(url_for('create_qr'))

                import uuid as uuid_module
                image_filename = f"{uuid_module.uuid4().hex}_{image_file.filename}"
                image_filename = image_filename.replace(' ', '_')
                image_save_path = os.path.join(app.config['UPLOAD_FOLDER'], 'qr_images', image_filename)
                image_file.save(image_save_path)

                content['image_path'] = os.path.join('qr_images', image_filename).replace('\\', '/')
                content['caption'] = request.form.get('image_caption', '').strip()
                content['bg_color_1'] = request.form.get('img_bg_color_1', '#0a0a0a').strip()
                content['bg_color_2'] = request.form.get('img_bg_color_2', '#1a1a2e').strip()
                content['bg_direction'] = request.form.get('img_bg_direction', 'to bottom').strip()
                print(f"Image QR - Path: {content['image_path']}, Caption: {content['caption']}")

            # Basic QR code styling options with validation
            template = request.form.get('template', '')
            color = request.form.get('color', '#000000').strip()
            if not color or color == 'undefined' or color == 'null':
                color = '#000000'
            
            background_color = request.form.get('background_color', '#FFFFFF').strip()
            if not background_color or background_color == 'undefined':
                background_color = '#FFFFFF'
                
            shape = request.form.get('shape', 'square')
            frame_type = request.form.get('frame_type', '')
            frame_text = request.form.get('frame_text', '')
            
            print(f"Styling - Template: {template}, Color: {color}, Shape: {shape}")
            
            # Get frame color if a frame is selected
            frame_color = None
            if frame_type:
                frame_color = request.form.get('frame_color', '#000000')
                if not frame_color or frame_color.strip() == '':
                    frame_color = '#000000'
            
            # ENHANCED GRADIENT DETECTION AND HANDLING
            using_gradient = False
            gradient_start = ''
            gradient_end = ''
            
            # Check if user explicitly selected gradient
            export_type = request.form.get('export_type', 'png')
            if export_type == 'gradient':
                using_gradient = True
                print("Gradient enabled via export_type")
            
            # Check if gradient is enabled via checkbox or form field
            if 'using_gradient' in request.form and request.form.get('using_gradient') == 'true':
                using_gradient = True
                print("Gradient enabled via form field")
            
            # Check if template has gradient and user didn't override
            if template and template in QR_TEMPLATES:
                template_config = QR_TEMPLATES[template]
                if template_config.get('export_type') == 'gradient':
                    using_gradient = True
                    # Use template gradient colors if not provided by user
                    gradient_start = request.form.get('gradient_start', template_config.get('gradient_start', '#f97316'))
                    gradient_end = request.form.get('gradient_end', template_config.get('gradient_end', '#fbbf24'))
                    print("Gradient enabled via template")
            
            # Get gradient colors from form if using gradient
            if using_gradient:
                gradient_start = request.form.get('gradient_start', gradient_start or '#f97316')
                gradient_end = request.form.get('gradient_end', gradient_end or '#fbbf24')
                export_type = 'gradient'  # Force export type to gradient
            
            print(f"Gradient settings - Using: {using_gradient}, Start: {gradient_start}, End: {gradient_end}")
            
            # Handle the gradient → custom eyes relationship
            eye_settings = handle_gradient_custom_eyes_relationship(request.form, using_gradient)
            
            # Use the returned values
            custom_eyes = eye_settings['custom_eyes']
            inner_eye_color = eye_settings['inner_eye_color']
            outer_eye_color = eye_settings['outer_eye_color']
            inner_eye_style = eye_settings['inner_eye_style']
            outer_eye_style = eye_settings['outer_eye_style']
            
            print(f"Eye settings - Custom eyes: {custom_eyes}, Inner: {inner_eye_color}, Outer: {outer_eye_color}")
            
            # Advanced settings
            module_size = int(request.form.get('module_size', 10))
            quiet_zone = int(request.form.get('quiet_zone', 4))
            error_correction = request.form.get('error_correction', 'H')
            watermark_text = request.form.get('watermark_text', '')
            
            # Gradient settings
            gradient_type = request.form.get('gradient_type', 'linear')
            gradient_direction = request.form.get('gradient_direction', 'to-right')
            
            # Logo settings
            logo_size_percentage = int(request.form.get('logo_size_percentage', 25))
            round_logo = 'round_logo' in request.form and request.form.get('round_logo') == 'true'
            
            print(f"Advanced settings - Module: {module_size}, Quiet: {quiet_zone}, Logo size: {logo_size_percentage}%")
            
            # Create base QR code record with all settings
            new_qr = QRCode(
                unique_id=str(uuid.uuid4()),
                name=name,
                qr_type=qr_type,
                is_dynamic=is_dynamic,
                content=json.dumps(content),  # Store the actual content
                color=color,
                background_color=background_color,
                frame_type=frame_type,
                frame_color=frame_color,
                shape=shape,
                template=template,
                custom_eyes=custom_eyes,
                inner_eye_style=inner_eye_style,
                outer_eye_style=outer_eye_style,
                inner_eye_color=inner_eye_color,
                outer_eye_color=outer_eye_color,
                module_size=module_size,
                quiet_zone=quiet_zone,
                error_correction=error_correction,
                export_type=export_type,
                watermark_text=watermark_text,
                
                # CRITICAL: Gradient column handling
                gradient=using_gradient,  # Set based on comprehensive detection
                
                gradient_start=gradient_start if using_gradient else None,
                gradient_end=gradient_end if using_gradient else None,
                gradient_type=gradient_type if using_gradient else None,
                gradient_direction=gradient_direction if using_gradient else None,
                logo_size_percentage=logo_size_percentage,
                round_logo=round_logo,
                frame_text=frame_text,
                user_id=current_user.id
            )
            
            print(f"QR object created with ID: {new_qr.unique_id}")
            
            # Apply template with gradient support if specified
            if template:
                apply_template_with_gradient_support(new_qr, template)
                print(f"Template applied: {template}")
            
            # ENHANCED LOGO HANDLING WITH EXTENSIVE DEBUGGING
            logo_path = None
            try:
                print("=== LOGO HANDLING START ===")
                logo_path = fix_logo_path_handling(request, new_qr)
                new_qr.logo_path = logo_path
                
                if logo_path:
                    print(f"Logo path set for new QR: {logo_path}")
                    
                    # Verify logo was saved correctly
                    upload_folder = app.config.get('UPLOAD_FOLDER', 'static/uploads')
                    
                    # Try multiple possible full paths
                    possible_full_paths = [
                        logo_path,  # If already absolute
                        os.path.join(upload_folder, logo_path),  # Relative to upload folder
                        os.path.abspath(logo_path),  # Make absolute
                        os.path.abspath(os.path.join(upload_folder, logo_path))  # Absolute + upload folder
                    ]
                    
                    logo_verified = False
                    for full_path in possible_full_paths:
                        if os.path.exists(full_path) and os.path.getsize(full_path) > 0:
                            print(f"Logo verified at: {full_path}")
                            logo_verified = True
                            break
                    
                    if not logo_verified:
                        print(f"Warning: Logo file not found after save at any of: {possible_full_paths}")
                        new_qr.logo_path = None
                        flash('Logo upload failed - continuing without logo', 'warning')
                    else:
                        print(f"Logo successfully saved and verified")
                        # Logo success notification handled by JavaScript on create page
                else:
                    print("No logo uploaded or logo handling failed")
                    
            except Exception as logo_error:
                print(f"Error in logo handling: {str(logo_error)}")
                import traceback
                traceback.print_exc()
                new_qr.logo_path = None
                flash('Logo upload failed - continuing without logo', 'warning')
            
            print("=== LOGO HANDLING END ===")

            # Save the base QR record to get an ID
            db.session.add(new_qr)
            db.session.flush()  # Get ID without committing
            
            print(f"QR base record saved with database ID: {new_qr.id}")
            
            # Now create the specific QR type record based on the type
            try:
                if qr_type == 'link':
                    link_detail = QRLink(
                        qr_code_id=new_qr.id,
                        url=content['url']
                    )
                    db.session.add(link_detail)
                    print("Link detail record created")
                    
                elif qr_type == 'email':
                    email_detail = QREmail(
                        qr_code_id=new_qr.id,
                        email=content['email'],
                        subject=content.get('subject', ''),
                        body=content.get('body', '')
                    )
                    db.session.add(email_detail)
                    print("Email detail record created")
                    
                elif qr_type == 'text':
                    text_detail = QRText(
                        qr_code_id=new_qr.id,
                        text=content['text']
                    )
                    db.session.add(text_detail)
                    print("Text detail record created")
                    
                elif qr_type == 'call':
                    phone_detail = QRPhone(
                        qr_code_id=new_qr.id,
                        phone=content['phone']
                    )
                    db.session.add(phone_detail)
                    print("Phone detail record created")
                    
                elif qr_type == 'sms':
                    sms_detail = QRSms(
                        qr_code_id=new_qr.id,
                        phone=content['phone'],
                        message=content.get('message', '')
                    )
                    db.session.add(sms_detail)
                    print("SMS detail record created")
                    
                elif qr_type == 'whatsapp':
                    whatsapp_detail = QRWhatsApp(
                        qr_code_id=new_qr.id,
                        phone=content['phone'],
                        message=content.get('message', '')
                    )
                    db.session.add(whatsapp_detail)
                    print("WhatsApp detail record created")
                    
                elif qr_type == 'wifi':
                    wifi_detail = QRWifi(
                        qr_code_id=new_qr.id,
                        ssid=content['ssid'],
                        password=content.get('password', ''),
                        encryption=content.get('encryption', 'WPA')
                    )
                    db.session.add(wifi_detail)
                    print("WiFi detail record created")
                    
                elif qr_type == 'vcard':
                    # Handle social media data
                    social_media_data = content.get('social_media', {})
                    
                    vcard_detail = QRVCard(
                        qr_code_id=new_qr.id,
                        name=content['name'],
                        phone=content.get('phone', ''),
                        email=content.get('email', ''),
                        company=content.get('company', ''),
                        title=content.get('title', ''),
                        address=content.get('address', ''),
                        website=content.get('website', ''),
                        primary_color=content.get('primary_color', '#2c5282'),
                        secondary_color=content.get('secondary_color', '#3182ce'),
                        social_media=json.dumps(social_media_data) if social_media_data else None
                    )
                    
                    # Handle vCard logo if provided
                    vcard_logo = request.files.get('vcard_logo')
                    if vcard_logo and vcard_logo.filename:
                        try:
                            logo_filename = secure_filename(vcard_logo.filename)
                            vcard_logo_path = os.path.join(app.config['UPLOAD_FOLDER'], 'vcard', logo_filename)
                            os.makedirs(os.path.dirname(vcard_logo_path), exist_ok=True)
                            vcard_logo.save(vcard_logo_path)
                            vcard_detail.logo_path = os.path.join('vcard', logo_filename)
                            print(f"vCard logo saved: {vcard_detail.logo_path}")
                        except Exception as vcard_logo_error:
                            print(f"vCard logo save failed: {vcard_logo_error}")
                    
                    db.session.add(vcard_detail)
                    print("vCard detail record created")
                    
                elif qr_type == 'event':
                    # Handle datetime fields
                    start_date = None
                    end_time = None
                    
                    start_date_str = content.get('start_date', '')
                    if start_date_str:
                        try:
                            start_date = datetime.fromisoformat(start_date_str.replace('Z', '+00:00'))
                        except ValueError:
                            try:
                                start_date = datetime.strptime(start_date_str, '%Y-%m-%dT%H:%M')
                            except ValueError:
                                print(f"Could not parse start date: {start_date_str}")
                    
                    end_time_str = content.get('end_time', '')
                    if end_time_str:
                        try:
                            end_time = datetime.fromisoformat(end_time_str.replace('Z', '+00:00'))
                        except ValueError:
                            try:
                                end_time = datetime.strptime(end_time_str, '%Y-%m-%dT%H:%M')
                            except ValueError:
                                print(f"Could not parse end time: {end_time_str}")
                    
                    event_detail = QREvent(
                        qr_code_id=new_qr.id,
                        title=content['title'],
                        location=content.get('location', ''),
                        start_date=start_date or datetime.now(UTC),  # Default to now if parsing fails
                        end_time=end_time,
                        description=content.get('description', ''),
                        organizer=content.get('organizer', '')
                    )
                    db.session.add(event_detail)
                    print(f"Event detail record created - Description: '{content.get('description', '')}'")
                    print("Event detail record created")

                elif qr_type == 'image':
                    image_detail = QRImage(
                        qr_code_id=new_qr.id,
                        image_path=content.get('image_path', ''),
                        caption=content.get('caption', ''),
                        bg_color_1=content.get('bg_color_1', '#0a0a0a'),
                        bg_color_2=content.get('bg_color_2', '#1a1a2e'),
                        bg_direction=content.get('bg_direction', 'to bottom')
                    )
                    db.session.add(image_detail)
                    print("Image detail record created")

            except Exception as detail_error:
                print(f"Error creating detail record: {str(detail_error)}")
                import traceback
                traceback.print_exc()
                db.session.rollback()
                flash(f'Error creating QR code details: {str(detail_error)}', 'error')
                return redirect(url_for('create_qr'))
            
            # Increment QR usage for the subscription
            try:
                active_subscription.qr_generated += 1
                print(f"Incremented QR usage to: {active_subscription.qr_generated}")
            except Exception as usage_error:
                print(f"Error incrementing usage: {usage_error}")
            
            # Commit all changes
            try:
                db.session.commit()
                print("All database changes committed successfully")
            except Exception as commit_error:
                print(f"Database commit failed: {str(commit_error)}")
                db.session.rollback()
                flash(f'Error saving QR code: {str(commit_error)}', 'error')
                return redirect(url_for('create_qr'))
            
            print(f"=== CREATE QR CODE SUCCESS ===")
            print(f"QR Code created with unique ID: {new_qr.unique_id}")
            
            flash('QR Code created successfully!', 'success')
            return redirect(url_for('view_qr', qr_id=new_qr.unique_id))
            
        except Exception as e:
            import traceback
            app.logger.error(f"Error creating QR code: {str(e)}")
            app.logger.error(traceback.format_exc())
            print(f"=== CREATE QR CODE ERROR ===")
            print(f"Error: {str(e)}")
            print(traceback.format_exc())
            
            try:
                db.session.rollback()
                print("Database rolled back")
            except:
                pass
                
            flash(f'Error creating QR code: {str(e)}', 'error')
            return redirect(url_for('create_qr'))
    
    # GET request - show the form
    print(f"=== SHOWING CREATE QR FORM ===")
    print(f"User ID: {user_id}")
    
    # Get available templates for the user's subscription
    available_templates = []
    if active_subscription and active_subscription.subscription.design:
        available_templates = active_subscription.subscription.get_designs()
        print(f"Available templates: {available_templates}")
    
    # Calculate QR limits and usage
    qr_limit = 0
    qr_used = 0
    qr_remaining = 0
    
    if active_subscription:
        qr_limit = active_subscription.effective_qr_limit
        qr_used = active_subscription.qr_generated
        qr_remaining = max(0, qr_limit - qr_used)
    
    print(f"QR Usage - Used: {qr_used}, Limit: {qr_limit}, Remaining: {qr_remaining}")
    
    # FIXED: Enhanced dynamic QR check - allow dynamic QR for dynamic plans
    can_create_dynamic = False
    if active_subscription:
        subscription_plan = active_subscription.subscription
        plan_type_lower = subscription_plan.plan_type.lower()
        plan_name_lower = subscription_plan.plan.lower()

        # Allow dynamic QR codes if:
        # 1. Plan type is specifically "Dynamic" (case insensitive), OR
        # 2. Plan type is not "Normal" and tier >= 2, OR
        # 3. Plan name contains "dynamic" (case insensitive)
        can_create_dynamic = (
            plan_type_lower == 'dynamic' or
            'dynamic' in plan_name_lower or
            (plan_type_lower != 'normal' and subscription_plan.tier >= 2)
        )
    
    print(f"Can create dynamic: {can_create_dynamic}")
    print(f"Plan type: {active_subscription.subscription.plan_type if active_subscription else 'None'}")
    print(f"Tier: {active_subscription.subscription.tier if active_subscription else 'None'}")
    print(f"=== CREATE QR FORM READY ===")
    
    return render_template('create_qr.html', 
                          qr_templates=QR_TEMPLATES,
                          available_templates=available_templates,
                          qr_limit=qr_limit,
                          qr_used=qr_used,
                          qr_remaining=qr_remaining,
                          can_create_dynamic=can_create_dynamic)
@app.route('/preview-qr', methods=['GET', 'POST'])
def preview_qr():
    from flask import Response
    
    try:
        print("=== PREVIEW QR DEBUG ===")
        
        # Check for user authentication
        if not current_user.is_authenticated:
            temp_img = qrcode.make("Example QR Code").get_image()
            buffered = BytesIO()
            temp_img.save(buffered, format="PNG")
            img_data = buffered.getvalue()
            return Response(img_data, mimetype='image/png')
        
        # Extract parameters from either form data (POST) or query string (GET)
        if request.method == 'POST':
            print("Form data received:")
            for key, value in request.form.items():
                if not isinstance(value, str) or len(str(value)) < 100:
                    print(f"  {key}: {value}")
                else:
                    print(f"  {key}: [long value truncated]")
                    
            # Get basic parameters
            qr_type = request.form.get('qr_type', 'link')
            name = request.form.get('name', 'Preview')
            is_dynamic = 'is_dynamic' in request.form and request.form.get('is_dynamic') == 'true'
            
            # Get styling parameters
            color = request.form.get('color', '#000000')
            if not color or color.strip() == '' or color == 'undefined':
                color = '#000000'
            background_color = request.form.get('background_color', '#FFFFFF')
            if not background_color or background_color.strip() == '' or background_color == 'undefined':
                background_color = '#FFFFFF'
                
            shape = request.form.get('shape', 'square')
            template = request.form.get('template', '')
            frame_type = request.form.get('frame_type', '')
            frame_text = request.form.get('frame_text', '')
            
            # Custom eyes
            custom_eyes = 'custom_eyes' in request.form and request.form.get('custom_eyes') == 'true'
            inner_eye_style = request.form.get('inner_eye_style', '')
            outer_eye_style = request.form.get('outer_eye_style', '')
            inner_eye_color = request.form.get('inner_eye_color', '')
            outer_eye_color = request.form.get('outer_eye_color', '')
            
            # Advanced parameters
            module_size = int(request.form.get('module_size', 10))
            quiet_zone = int(request.form.get('quiet_zone', 4))
            error_correction = request.form.get('error_correction', 'H')
            
            # CRITICAL FIX: Simplified gradient detection
            export_type = request.form.get('export_type', 'png')
            using_gradient_form = request.form.get('using_gradient', 'false').lower() == 'true'
            
            print(f"Export type: {export_type}")
            print(f"Using gradient form field: {using_gradient_form}")
            
            # Simple and clear logic for gradient detection
            if export_type == 'gradient' or using_gradient_form:
                using_gradient = True
                gradient_start = request.form.get('gradient_start', '#f97316')
                gradient_end = request.form.get('gradient_end', '#fbbf24')
                print(f"GRADIENT MODE: {gradient_start} -> {gradient_end}")
            else:
                using_gradient = False
                gradient_start = ''
                gradient_end = ''
                print("SOLID COLOR MODE")
            
            watermark_text = request.form.get('watermark_text', '')
            logo_size_percentage = int(request.form.get('logo_size_percentage', 25))
            round_logo = 'round_logo' in request.form and request.form.get('round_logo') == 'true'
            
        else:  # GET request
            # Similar logic for GET requests...
            qr_type = request.args.get('qr_type', 'link')
            name = request.args.get('name', 'Preview')
            is_dynamic = request.args.get('is_dynamic', 'false').lower() == 'true'
            
            color = request.args.get('color', '#000000')
            if not color or color.strip() == '' or color == 'undefined':
                color = '#000000'
            background_color = request.args.get('background_color', '#FFFFFF')
            if not background_color or background_color.strip() == '' or background_color == 'undefined':
                background_color = '#FFFFFF'
                
            shape = request.args.get('shape', 'square')
            template = request.args.get('template', '')
            frame_type = request.args.get('frame_type', '')
            frame_text = request.args.get('frame_text', '')
            
            custom_eyes = request.args.get('custom_eyes', 'false').lower() == 'true'
            inner_eye_style = request.args.get('inner_eye_style', '')
            outer_eye_style = request.args.get('outer_eye_style', '')
            inner_eye_color = request.args.get('inner_eye_color', '')
            outer_eye_color = request.args.get('outer_eye_color', '')
            
            module_size = int(request.args.get('module_size', 10))
            quiet_zone = int(request.args.get('quiet_zone', 4))
            error_correction = request.args.get('error_correction', 'H')
            
            # Gradient detection for GET
            export_type = request.args.get('export_type', 'png')
            using_gradient_form = request.args.get('using_gradient', 'false').lower() == 'true'
            
            if export_type == 'gradient' or using_gradient_form:
                using_gradient = True
                gradient_start = request.args.get('gradient_start', '#f97316')
                gradient_end = request.args.get('gradient_end', '#fbbf24')
            else:
                using_gradient = False
                gradient_start = ''
                gradient_end = ''
                
            watermark_text = request.args.get('watermark_text', '')
            logo_size_percentage = int(request.args.get('logo_size_percentage', 25))
            round_logo = request.args.get('round_logo', 'false').lower() == 'true'
        
        # Handle ID parameter for editing existing QR codes
        qr_id = request.form.get('id', '') if request.method == 'POST' else request.args.get('id', '')
        base_qr_code = None
        
        if qr_id:
            base_qr_code = QRCode.query.filter_by(unique_id=qr_id).first()
            if base_qr_code and base_qr_code.user_id == current_user.id:
                print(f"Found existing QR code for preview: {qr_id}")
                qr_type = base_qr_code.qr_type
                
                # Use existing styling only if not overridden by form parameters
                if request.method == 'GET':
                    color = base_qr_code.color or color
                    background_color = base_qr_code.background_color or background_color
                    shape = base_qr_code.shape or shape
                    template = base_qr_code.template or template
                    frame_type = base_qr_code.frame_type or frame_type
                    frame_text = base_qr_code.frame_text or frame_text
                    custom_eyes = base_qr_code.custom_eyes if base_qr_code.custom_eyes is not None else custom_eyes
                    inner_eye_style = base_qr_code.inner_eye_style or inner_eye_style
                    outer_eye_style = base_qr_code.outer_eye_style or outer_eye_style
                    inner_eye_color = base_qr_code.inner_eye_color or inner_eye_color
                    outer_eye_color = base_qr_code.outer_eye_color or outer_eye_color
                    module_size = base_qr_code.module_size or module_size
                    quiet_zone = base_qr_code.quiet_zone or quiet_zone
                    error_correction = base_qr_code.error_correction or error_correction
                    watermark_text = base_qr_code.watermark_text or watermark_text
                    logo_size_percentage = base_qr_code.logo_size_percentage or logo_size_percentage
                    round_logo = base_qr_code.round_logo if base_qr_code.round_logo is not None else round_logo
                    
                    # Handle gradient settings from existing QR
                    if base_qr_code.gradient and not using_gradient:
                        using_gradient = True
                        gradient_start = base_qr_code.gradient_start or gradient_start
                        gradient_end = base_qr_code.gradient_end or gradient_end
                        export_type = 'gradient'
        
        # Build content based on QR type
        content = {}
        if qr_type == 'link':
            if base_qr_code and hasattr(base_qr_code, 'link_detail') and base_qr_code.link_detail:
                content['url'] = request.form.get('url', base_qr_code.link_detail.url) if request.method == 'POST' else base_qr_code.link_detail.url
            else:
                content['url'] = request.form.get('url', 'https://example.com') if request.method == 'POST' else request.args.get('url', 'https://example.com')
                
        elif qr_type == 'email':
            if base_qr_code and hasattr(base_qr_code, 'email_detail') and base_qr_code.email_detail:
                content['email'] = request.form.get('email', base_qr_code.email_detail.email) if request.method == 'POST' else base_qr_code.email_detail.email
                content['subject'] = request.form.get('subject', base_qr_code.email_detail.subject or '') if request.method == 'POST' else (base_qr_code.email_detail.subject or '')
                content['body'] = request.form.get('body', base_qr_code.email_detail.body or '') if request.method == 'POST' else (base_qr_code.email_detail.body or '')
            else:
                content['email'] = request.form.get('email', '') if request.method == 'POST' else request.args.get('email', 'example@email.com')
                content['subject'] = request.form.get('subject', '') if request.method == 'POST' else request.args.get('subject', '')
                content['body'] = request.form.get('body', '') if request.method == 'POST' else request.args.get('body', '')
                
        elif qr_type == 'text':
            if base_qr_code and hasattr(base_qr_code, 'text_detail') and base_qr_code.text_detail:
                content['text'] = request.form.get('text', base_qr_code.text_detail.text) if request.method == 'POST' else base_qr_code.text_detail.text
            else:
                content['text'] = request.form.get('text', '') if request.method == 'POST' else request.args.get('text', 'Sample text')
                
        elif qr_type == 'call':
            if base_qr_code and hasattr(base_qr_code, 'phone_detail') and base_qr_code.phone_detail:
                content['phone'] = request.form.get('phone', base_qr_code.phone_detail.phone) if request.method == 'POST' else base_qr_code.phone_detail.phone
            else:
                content['phone'] = request.form.get('phone', '') if request.method == 'POST' else request.args.get('phone', '+1234567890')
                
        elif qr_type == 'sms':
            if base_qr_code and hasattr(base_qr_code, 'sms_detail') and base_qr_code.sms_detail:
                content['phone'] = request.form.get('sms-phone', base_qr_code.sms_detail.phone) if request.method == 'POST' else base_qr_code.sms_detail.phone
                content['message'] = request.form.get('message', base_qr_code.sms_detail.message or '') if request.method == 'POST' else (base_qr_code.sms_detail.message or '')
            else:
                content['phone'] = request.form.get('sms-phone', '') if request.method == 'POST' else request.args.get('phone', '+1234567890')
                content['message'] = request.form.get('message', '') if request.method == 'POST' else request.args.get('message', 'Hello!')
                
        elif qr_type == 'whatsapp':
            if base_qr_code and hasattr(base_qr_code, 'whatsapp_detail') and base_qr_code.whatsapp_detail:
                content['phone'] = request.form.get('whatsapp-phone', base_qr_code.whatsapp_detail.phone) if request.method == 'POST' else base_qr_code.whatsapp_detail.phone
                content['message'] = request.form.get('whatsapp-message', base_qr_code.whatsapp_detail.message or '') if request.method == 'POST' else (base_qr_code.whatsapp_detail.message or '')
            else:
                content['phone'] = request.form.get('whatsapp-phone', '') if request.method == 'POST' else request.args.get('phone', '+1234567890')
                content['message'] = request.form.get('whatsapp-message', '') if request.method == 'POST' else request.args.get('message', 'Hello!')
                
        elif qr_type == 'wifi':
            if base_qr_code and hasattr(base_qr_code, 'wifi_detail') and base_qr_code.wifi_detail:
                content['ssid'] = request.form.get('ssid', base_qr_code.wifi_detail.ssid) if request.method == 'POST' else base_qr_code.wifi_detail.ssid
                content['password'] = request.form.get('password', base_qr_code.wifi_detail.password or '') if request.method == 'POST' else (base_qr_code.wifi_detail.password or '')
                content['encryption'] = request.form.get('encryption', base_qr_code.wifi_detail.encryption) if request.method == 'POST' else base_qr_code.wifi_detail.encryption
            else:
                content['ssid'] = request.form.get('ssid', '') if request.method == 'POST' else request.args.get('ssid', 'WiFi-Network')
                content['password'] = request.form.get('password', '') if request.method == 'POST' else request.args.get('password', '')
                content['encryption'] = request.form.get('encryption', 'WPA') if request.method == 'POST' else request.args.get('encryption', 'WPA')
                
        elif qr_type == 'vcard':
            if base_qr_code and hasattr(base_qr_code, 'vcard_detail') and base_qr_code.vcard_detail:
                content['name'] = request.form.get('full_name', base_qr_code.vcard_detail.name) if request.method == 'POST' else base_qr_code.vcard_detail.name
                content['phone'] = request.form.get('vcard-phone', base_qr_code.vcard_detail.phone or '') if request.method == 'POST' else (base_qr_code.vcard_detail.phone or '')
                content['email'] = request.form.get('vcard-email', base_qr_code.vcard_detail.email or '') if request.method == 'POST' else (base_qr_code.vcard_detail.email or '')
                content['company'] = request.form.get('company', base_qr_code.vcard_detail.company or '') if request.method == 'POST' else (base_qr_code.vcard_detail.company or '')
            else:
                content['name'] = request.form.get('full_name', '') if request.method == 'POST' else request.args.get('name', 'John Doe')
                content['phone'] = request.form.get('vcard-phone', '') if request.method == 'POST' else request.args.get('phone', '+1234567890')
                content['email'] = request.form.get('vcard-email', '') if request.method == 'POST' else request.args.get('email', 'john@example.com')
                content['company'] = request.form.get('company', '') if request.method == 'POST' else request.args.get('company', 'Company')
                
        elif qr_type == 'event':
            if base_qr_code and hasattr(base_qr_code, 'event_detail') and base_qr_code.event_detail:
                content['title'] = request.form.get('event-title', base_qr_code.event_detail.title) if request.method == 'POST' else base_qr_code.event_detail.title
                content['location'] = request.form.get('location', base_qr_code.event_detail.location or '') if request.method == 'POST' else (base_qr_code.event_detail.location or '')
                content['start_date'] = base_qr_code.event_detail.start_date.isoformat() if base_qr_code.event_detail.start_date else ''
                content['end_time'] = base_qr_code.event_detail.end_time.isoformat() if base_qr_code.event_detail.end_time else ''
            else:
                content['title'] = request.form.get('event-title', '') if request.method == 'POST' else request.args.get('title', 'Sample Event')
                content['location'] = request.form.get('location', '') if request.method == 'POST' else request.args.get('location', 'Event Location')
                content['start_date'] = request.form.get('start_date', '') if request.method == 'POST' else request.args.get('start_date', '')
                content['end_time'] = request.form.get('end_time', '') if request.method == 'POST' else request.args.get('end_time', '')

        elif qr_type == 'image':
            if base_qr_code and hasattr(base_qr_code, 'image_detail') and base_qr_code.image_detail:
                content['image_path'] = base_qr_code.image_detail.image_path or ''
                content['caption'] = request.form.get('image_caption', base_qr_code.image_detail.caption or '') if request.method == 'POST' else (base_qr_code.image_detail.caption or '')
            else:
                content['image_path'] = ''
                content['caption'] = request.form.get('image_caption', '') if request.method == 'POST' else ''

        # Handle logo preview
        logo_file = None
        logo_path_for_preview = None
        
        if request.method == 'POST' and 'logo' in request.files and request.files['logo'].filename:
            logo_file = request.files['logo']
        elif base_qr_code and base_qr_code.logo_path:
            logo_path_for_preview = base_qr_code.logo_path
            
        print(f"Final gradient decision: using_gradient={using_gradient}, export_type={export_type}")
        print(f"Final colors: QR={color}, BG={background_color}, Gradient={gradient_start}->{gradient_end}")
        
        # Create temporary QR code object with all parameters
        temp_qr = QRCode(
            unique_id="preview",
            name=name,
            qr_type=qr_type,
            is_dynamic=is_dynamic,
            content=json.dumps(content),
            
            # Basic styling
            color=color,
            background_color=background_color,
            shape=shape,
            template=template,
            
            # Frame settings
            frame_type=frame_type,
            frame_text=frame_text,
            
            # Custom eyes
            custom_eyes=custom_eyes,
            inner_eye_style=inner_eye_style,
            outer_eye_style=outer_eye_style,
            inner_eye_color=inner_eye_color,
            outer_eye_color=outer_eye_color,
            
            # Advanced settings
            module_size=module_size,
            quiet_zone=quiet_zone,
            error_correction=error_correction,
            export_type=export_type,
            
            # CRITICAL: Gradient settings
            gradient=using_gradient,
            gradient_start=gradient_start if using_gradient else None,
            gradient_end=gradient_end if using_gradient else None,
            gradient_type='linear' if using_gradient else None,
            gradient_direction='to-right' if using_gradient else None,
            
            # Other settings
            watermark_text=watermark_text,
            logo_size_percentage=logo_size_percentage,
            round_logo=round_logo,
            user_id=current_user.id if current_user.is_authenticated else 0
        )
        
        # Handle temp logo file if uploaded
        temp_logo_path = None
        if logo_file:
            temp_logo_path = preview_qr_logo_handling(logo_file, temp_qr)
        elif logo_path_for_preview:
            temp_qr.logo_path = logo_path_for_preview
        
        print(f"About to generate QR with: template={template}, gradient={using_gradient}, export_type={export_type}")
        
        # Generate QR code using the complete pipeline
        qr_image, qr_info = generate_qr_code(temp_qr)
        
        print(f"Generated QR code successfully")
        
        # Clean up temporary logo file if it was created
        if temp_logo_path and os.path.exists(temp_logo_path):
            try:
                os.remove(temp_logo_path)
            except:
                pass
        
        # Convert response to binary
        if qr_image.startswith('data:'):
            header, encoded = qr_image.split(",", 1)
            img_data = base64.b64decode(encoded)
        else:
            img_data = base64.b64decode(qr_image)
        
        print(f"Returning preview image, size: {len(img_data)} bytes")
        print("=== END PREVIEW QR DEBUG ===")
        
        # Return the image
        return Response(img_data, mimetype='image/png')
    
    except Exception as e:
        app.logger.error(f"Error in preview_qr: {str(e)}")
        import traceback
        app.logger.error(traceback.format_exc())
        print(f"Preview error: {str(e)}")
        print(traceback.format_exc())
        
        # Return a fallback/error image
        try:
            error_qr = qrcode.make("Error generating QR preview").get_image()
            buffered = BytesIO()
            error_qr.save(buffered, format="PNG")
            img_data = buffered.getvalue()
            return Response(img_data, mimetype='image/png')
        except:
            return Response(status=500)
        
        
# 4. Fixed preview_qr function logo handling
def preview_qr_logo_handling(logo_file, temp_qr):
    """Handle logo file for QR preview - FIXED VERSION"""
    temp_logo_path = None
    
    if logo_file and hasattr(logo_file, 'filename') and logo_file.filename:
        try:
            # Create temp directory
            upload_folder = app.config.get('UPLOAD_FOLDER', 'static/uploads')
            temp_dir = os.path.join(upload_folder, 'temp')
            os.makedirs(temp_dir, exist_ok=True)
            
            # Validate file extension
            file_ext = os.path.splitext(logo_file.filename)[1].lower()
            valid_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']
            
            if not file_ext or file_ext not in valid_extensions:
                file_ext = '.png'
            
            # Create temporary file
            temp_filename = f"preview_{uuid.uuid4().hex[:8]}{file_ext}"
            temp_logo_path = os.path.join(temp_dir, temp_filename)
            
            # Save and validate
            logo_file.save(temp_logo_path)
            
            if not os.path.exists(temp_logo_path) or os.path.getsize(temp_logo_path) == 0:
                print(f"Preview logo save failed: {temp_logo_path}")
                return None
            
            # Validate image
            try:
                from PIL import Image
                test_img = Image.open(temp_logo_path)
                test_img.verify()
                print(f"Preview logo saved and validated: {temp_logo_path}")
            except Exception as img_error:
                print(f"Preview logo validation failed: {img_error}")
                if os.path.exists(temp_logo_path):
                    os.remove(temp_logo_path)
                return None
            
            # Update temp QR object
            temp_qr.logo_path = temp_logo_path
            
            return temp_logo_path
            
        except Exception as e:
            print(f"Error in preview logo handling: {str(e)}")
            if temp_logo_path and os.path.exists(temp_logo_path):
                try:
                    os.remove(temp_logo_path)
                except:
                    pass
    
    return None

@app.route('/qr/<qr_id>')
@login_required
def view_qr(qr_id):
    qr_code = QRCode.query.filter_by(unique_id=qr_id).first_or_404()
    
    # Ensure the QR code belongs to the current user
    if qr_code.user_id != current_user.id:
        flash('You do not have permission to view this QR code.')
        return redirect(url_for('dashboard'))

    # Generate QR code image
    qr_image, qr_info = generate_qr_code(qr_code)

    # Get scan statistics if dynamic
    scans = []
    if qr_code.is_dynamic:
        scans = Scan.query.filter_by(qr_code_id=qr_code.id).all()

    # ✅ Fix logo_path safely
    if qr_code.logo_path:
        if not qr_code.logo_path.startswith('uploads/'):
            qr_code.logo_path = os.path.join('uploads', qr_code.logo_path).replace('\\', '/')
        else:
            qr_code.logo_path = qr_code.logo_path.replace('\\', '/')
    else:
        qr_code.logo_path = None  # Or set to a default image path if you want

    # Parse the JSON content
    content = json.loads(qr_code.content)

    return render_template('view_qr.html', qr_code=qr_code, qr_image=qr_image, 
                           qr_info=qr_info, scans=scans, content=content)



# In your edit_qr route, add this section where you handle styling updates:

@app.route('/qr/<qr_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_qr(qr_id):
    """
    Complete edit QR route with comprehensive error handling and debugging
    FIXED: All database access, form preparation, and error scenarios
    """
    try:
        app.logger.info(f"Edit QR request for {qr_id} by user {current_user.id}")
        
        # 1. GET AND VALIDATE QR CODE
        qr_code = QRCode.query.filter_by(unique_id=qr_id).first()
        if not qr_code:
            app.logger.error(f"QR code not found: {qr_id}")
            flash('QR code not found.', 'error')
            return redirect(url_for('dashboard'))
        
        # 2. CHECK USER OWNERSHIP
        if qr_code.user_id != current_user.id:
            app.logger.warning(f"User {current_user.id} tried to edit QR {qr_id} owned by {qr_code.user_id}")
            flash('You do not have permission to edit this QR code.', 'danger')
            return redirect(url_for('dashboard'))
        
        app.logger.info(f"QR code validation passed for {qr_id}")
        
    except Exception as e:
        app.logger.error(f"Error accessing QR code {qr_id}: {str(e)}")
        flash('Error accessing QR code.', 'error')
        return redirect(url_for('dashboard'))
    
    # 3. HANDLE POST REQUEST (UPDATE QR CODE)
    if request.method == 'POST':
        try:
            app.logger.info(f"Processing POST request for QR {qr_id}")
            
            # Update basic QR code information
            new_name = request.form.get('name', '').strip()
            if new_name and new_name != qr_code.name:
                qr_code.name = new_name
                app.logger.info(f"Updated QR code name to: {new_name}")
            
            # Get and validate basic styling
            color = request.form.get('color', '#000000')
            if not color or color.strip() == '' or color in ['undefined', 'null']:
                color = '#000000'
            qr_code.color = color
            
            background_color = request.form.get('background_color', '#FFFFFF')
            if not background_color or background_color.strip() == '' or background_color == 'undefined':
                background_color = '#FFFFFF'
            qr_code.background_color = background_color
            
            # Update shape
            qr_code.shape = request.form.get('shape', 'square')
            
            # Handle gradient mode switching
            export_type = request.form.get('export_type', 'png')
            using_gradient_form = 'using_gradient' in request.form and request.form.get('using_gradient') == 'true'
            
            using_gradient = False
            if export_type == 'gradient':
                using_gradient = True
                app.logger.info("Gradient enabled via export_type = gradient")
            elif using_gradient_form:
                using_gradient = True
                app.logger.info("Gradient enabled via using_gradient form field")
            
            # Apply gradient settings or clear them
            if using_gradient:
                app.logger.info("Applying gradient settings")
                qr_code.gradient = True
                qr_code.export_type = 'gradient'
                qr_code.gradient_start = request.form.get('gradient_start', '#f97316')
                qr_code.gradient_end = request.form.get('gradient_end', '#fbbf24')
                qr_code.gradient_type = request.form.get('gradient_type', 'linear')
                qr_code.gradient_direction = request.form.get('gradient_direction', 'to-right')
                
                # Auto-enable custom eyes for gradient mode
                qr_code.custom_eyes = True
                
                # Set eye colors to match gradient
                inner_eye_color = request.form.get('inner_eye_color', '')
                outer_eye_color = request.form.get('outer_eye_color', '')
                
                if not inner_eye_color or inner_eye_color in ['', 'undefined', 'null']:
                    qr_code.inner_eye_color = qr_code.gradient_start
                else:
                    qr_code.inner_eye_color = inner_eye_color
                    
                if not outer_eye_color or outer_eye_color in ['', 'undefined', 'null']:
                    qr_code.outer_eye_color = qr_code.gradient_end
                else:
                    qr_code.outer_eye_color = outer_eye_color
                    
                qr_code.inner_eye_style = request.form.get('inner_eye_style', 'circle')
                qr_code.outer_eye_style = request.form.get('outer_eye_style', 'circle')
                
            else:
                app.logger.info("Switching to solid color mode")
                qr_code.gradient = False
                qr_code.export_type = 'png'
                qr_code.gradient_start = None
                qr_code.gradient_end = None
                qr_code.gradient_type = None
                qr_code.gradient_direction = None
                
                # Handle custom eyes for solid color mode
                custom_eyes = 'custom_eyes' in request.form and request.form.get('custom_eyes') == 'true'
                qr_code.custom_eyes = custom_eyes
                
                if custom_eyes:
                    inner_eye_color = request.form.get('inner_eye_color', '')
                    outer_eye_color = request.form.get('outer_eye_color', '')
                    
                    qr_code.inner_eye_color = inner_eye_color if inner_eye_color and inner_eye_color not in ['', 'undefined', 'null'] else color
                    qr_code.outer_eye_color = outer_eye_color if outer_eye_color and outer_eye_color not in ['', 'undefined', 'null'] else color
                    
                    qr_code.inner_eye_style = request.form.get('inner_eye_style', 'square')
                    qr_code.outer_eye_style = request.form.get('outer_eye_style', 'rounded')
                else:
                    qr_code.inner_eye_color = None
                    qr_code.outer_eye_color = None
                    qr_code.inner_eye_style = None
                    qr_code.outer_eye_style = None
            
            # Handle frame settings
            frame_type = request.form.get('frame_type', '')
            if frame_type and frame_type.strip() != '':
                qr_code.frame_type = frame_type
                qr_code.frame_text = request.form.get('frame_text', '')
                qr_code.frame_color = request.form.get('frame_color', '#000000')
                app.logger.info(f"Applied frame: {frame_type}")
            else:
                qr_code.frame_type = None
                qr_code.frame_text = None
                qr_code.frame_color = None
                app.logger.info("Cleared frame settings")
            
            # Apply template if specified
            template = request.form.get('template', '')
            if template:
                qr_code.frame_type = None
                qr_code.frame_color = None
                qr_code.frame_text = None
                apply_template_with_gradient_support(qr_code, template)
                app.logger.info(f"Applied template: {template}")
            
            # Update other styling parameters
            qr_code.module_size = request.form.get('module_size', 10, type=int)
            qr_code.quiet_zone = request.form.get('quiet_zone', 4, type=int)
            qr_code.error_correction = request.form.get('error_correction', 'H')
            qr_code.watermark_text = request.form.get('watermark_text', '')
            qr_code.logo_size_percentage = request.form.get('logo_size_percentage', 25, type=int)
            qr_code.round_logo = 'round_logo' in request.form and request.form.get('round_logo') == 'true'
            
            # Handle logo update
            try:
                logo_path = fix_logo_path_handling(request, qr_code)
                if logo_path != qr_code.logo_path:
                    qr_code.logo_path = logo_path
                    app.logger.info(f"Updated logo path to: {logo_path}")
            except Exception as logo_error:
                app.logger.error(f"Logo handling error: {str(logo_error)}")
            
            # Update content based on QR type
            content_updated = update_qr_content_by_type(qr_code, request.form)
            
            # Update timestamp
            qr_code.updated_at = datetime.utcnow()
            
            # Commit changes
            db.session.commit()
            
            app.logger.info(f"Edit completed for QR {qr_id}")
            
            if content_updated:
                flash('QR Code content and styling updated successfully!', 'success')
            else:
                flash('QR Code styling updated successfully!', 'success')
                
            return redirect(url_for('view_qr', qr_id=qr_id))
            
        except Exception as e:
            import traceback
            app.logger.error(f"Error updating QR code {qr_id}: {str(e)}")
            app.logger.error(traceback.format_exc())
            db.session.rollback()
            flash(f'Error updating QR code: {str(e)}', 'error')
            return redirect(url_for('edit_qr', qr_id=qr_id))
    
    # 4. HANDLE GET REQUEST (SHOW EDIT FORM)
    try:
        app.logger.info(f"Preparing edit form for QR {qr_id}")
        
        # Parse existing content
        content = safe_content_parsing(qr_code)
        
        # Get scan data
        scans = safe_scan_retrieval(qr_code.id)
        
        # Generate QR image
        qr_image, qr_info = safe_qr_generation(qr_code)
        
        # Get subscription info
        user_id = current_user.id
        active_subscription, available_templates = safe_subscription_check(user_id)
        
        # Prepare form data
        form_data = safe_form_data_preparation(qr_code)
        
        # Set maximum scans for display
        max_scans = 1000
        
        app.logger.info(f"Edit form data prepared successfully for QR {qr_id}")
        
        # Render template
        return render_template('edit_qr.html', 
                             qr_code=qr_code, 
                             content=content, 
                             qr_templates=QR_TEMPLATES,
                             available_templates=available_templates,
                             scans=scans,
                             qr_image=qr_image,
                             qr_info=qr_info,
                             max_scans=max_scans,
                             form_data=form_data)
                             
    except Exception as e:
        app.logger.error(f"Error preparing edit form for QR {qr_id}: {str(e)}")
        import traceback
        app.logger.error(traceback.format_exc())
        flash(f'Error loading QR code for editing: {str(e)}', 'error')
        return redirect(url_for('dashboard'))

@app.route('/qr/<qr_id>/delete', methods=['POST'])
@login_required
def delete_qr(qr_id):
    qr_code = QRCode.query.filter_by(unique_id=qr_id).first_or_404()
    
    # Ensure the QR code belongs to the current user
    if qr_code.user_id != current_user.id:
        flash('You do not have permission to delete this QR code.')
        return redirect(url_for('dashboard'))
    
    try:
        # Delete type-specific detail records first
        qr_type = qr_code.qr_type
        if qr_type == 'link':
            QRLink.query.filter_by(qr_code_id=qr_code.id).delete()
        elif qr_type == 'email':
            QREmail.query.filter_by(qr_code_id=qr_code.id).delete()
        elif qr_type == 'text':
            QRText.query.filter_by(qr_code_id=qr_code.id).delete()
        elif qr_type == 'call':
            QRPhone.query.filter_by(qr_code_id=qr_code.id).delete()
        elif qr_type == 'sms':
            QRSms.query.filter_by(qr_code_id=qr_code.id).delete()
        elif qr_type == 'whatsapp':
            QRWhatsApp.query.filter_by(qr_code_id=qr_code.id).delete()
        elif qr_type == 'wifi':
            QRWifi.query.filter_by(qr_code_id=qr_code.id).delete()
        elif qr_type == 'vcard':
            QRVCard.query.filter_by(qr_code_id=qr_code.id).delete()
        elif qr_type == 'event':
            QREvent.query.filter_by(qr_code_id=qr_code.id).delete()
        elif qr_type == 'image':
            image_detail = QRImage.query.filter_by(qr_code_id=qr_code.id).first()
            if image_detail and image_detail.image_path:
                img_path = os.path.join(app.config['UPLOAD_FOLDER'], image_detail.image_path)
                if os.path.exists(img_path):
                    try:
                        os.remove(img_path)
                    except:
                        pass
            QRImage.query.filter_by(qr_code_id=qr_code.id).delete()

        # Delete related scans
        Scan.query.filter_by(qr_code_id=qr_code.id).delete()
        
        # Remove logo file if it exists
        if qr_code.logo_path and os.path.exists(qr_code.logo_path):
            try:
                os.remove(qr_code.logo_path)
            except:
                pass
        
        # Delete the QR code
        db.session.delete(qr_code)
        db.session.commit()
        
        flash('QR Code deleted successfully!')
        return redirect(url_for('dashboard'))
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting QR code: {str(e)}', 'error')
        return redirect(url_for('dashboard'))

@app.route('/qr/<qr_id>/download')
def download_qr(qr_id):
    """Fixed download function that properly handles gradients and all styling options"""
    qr_code = QRCode.query.filter_by(unique_id=qr_id).first_or_404()
    
    try:
        # Use the complete QR generation pipeline to ensure all styling is applied
        qr_image_data, qr_info = generate_qr_code(qr_code)
        
        # Convert base64 data URL to binary data
        if qr_image_data.startswith('data:image/png;base64,'):
            # Extract just the base64 part
            header, encoded = qr_image_data.split(",", 1)
            img_data = base64.b64decode(encoded)
        else:
            # If it's just base64 without data URL prefix
            img_data = base64.b64decode(qr_image_data)
        
        # Create BytesIO buffer for the image
        buffer = BytesIO(img_data)
        buffer.seek(0)
        
        # Determine filename based on QR code name and format
        safe_name = "".join(c for c in qr_code.name if c.isalnum() or c in (' ', '-', '_')).rstrip()
        filename = f"{safe_name}.png"
        
        # Return the image file with proper headers
        return send_file(
            buffer, 
            mimetype='image/png',
            as_attachment=True, 
            download_name=filename
        )
        
    except Exception as e:
        app.logger.error(f"Error in download_qr: {str(e)}")
        import traceback
        app.logger.error(traceback.format_exc())
        
        # Fallback: create a simple QR code if the full pipeline fails
        try:
            qr_data = generate_qr_data(qr_code)
            
            # Create basic QR code
            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_H,
                box_size=qr_code.module_size or 10,
                border=qr_code.quiet_zone or 4
            )
            
            qr.add_data(qr_data)
            qr.make(fit=True)
            
            # Use basic colors
            color = qr_code.color if qr_code.color else '#000000'
            bg_color = qr_code.background_color if qr_code.background_color else '#FFFFFF'
            
            qr_img = qr.make_image(fill_color=color, back_color=bg_color)
            
            # Convert to BytesIO for download
            buffered = BytesIO()
            qr_img.save(buffered, format="PNG")
            buffered.seek(0)
            
            safe_name = "".join(c for c in qr_code.name if c.isalnum() or c in (' ', '-', '_')).rstrip()
            filename = f"{safe_name}_basic.png"
            
            return send_file(
                buffered, 
                mimetype='image/png',
                as_attachment=True, 
                download_name=filename
            )
            
        except Exception as fallback_error:
            app.logger.error(f"Fallback download also failed: {str(fallback_error)}")
            flash('Error generating QR code for download. Please try again.', 'error')
            return redirect(url_for('view_qr', qr_id=qr_id))

@app.route('/debug-qr-color/<qr_id>')
def debug_qr_color(qr_id):
    """Debug endpoint to check color values"""
    qr_code = QRCode.query.filter_by(unique_id=qr_id).first_or_404()
    
    color_info = {
        'qr_id': qr_id,
        'color_in_db': qr_code.color,
        'color_in_db_stripped': qr_code.color.strip() if qr_code.color else None,
        'background_color': qr_code.background_color,
        'hex_to_rgb_conversion': hex_to_rgb(qr_code.color) if qr_code.color else None,
    }
    
    return jsonify(color_info)



@app.route('/qr/<qr_id>/analytics')
@login_required
def qr_analytics(qr_id):
    user_id = current_user.id
    qr_code = QRCode.query.filter_by(unique_id=qr_id).first_or_404()
    
    # Ensure the QR code belongs to the current user
    if qr_code.user_id != user_id:
        flash('You do not have permission to view analytics for this QR code.', 'danger')
        return redirect(url_for('dashboard'))
    
    # Check if user has an active subscription
    active_subscription = (
        SubscribedUser.query
        .filter(SubscribedUser.U_ID == user_id)
        .filter(SubscribedUser.end_date > datetime.now(UTC))
        .filter(SubscribedUser._is_active == True)
        .first()
    )
    
    if not active_subscription:
        flash('You need an active subscription to view analytics.', 'warning')
        return redirect(url_for('subscription.user_subscriptions'))
    
    # Check if user has analytics available
    if active_subscription.analytics_used >= active_subscription.effective_analytics_limit:
        flash('You have reached your analytics usage limit for this subscription plan.', 'warning')
        return redirect(url_for('subscription.user_subscriptions'))
    
    # Increment analytics usage
    active_subscription.analytics_used += 1
    db.session.commit()
    
    # Get all scans for this QR code
    scans = Scan.query.filter_by(qr_code_id=qr_code.id).order_by(Scan.timestamp.desc()).all()
    total_scans = len(scans)
    
    # Get user's timezone preference
    user_timezone = session.get('user_timezone', app.config.get('DEFAULT_TIMEZONE', 'UTC'))
    
    # Add localized timestamps to scan objects
    for scan in scans:
        scan.localized_timestamp = get_localized_datetime(scan.timestamp, user_timezone)
    
    # Pagination for recent scans
    page = request.args.get('page', 1, type=int)
    page_size = 10
    total_pages = (total_scans + page_size - 1) // page_size if total_scans > 0 else 1
    page_num = min(max(page, 1), total_pages) if total_pages > 0 else 1
    
    # Get paginated scans
    start_index = (page_num - 1) * page_size
    end_index = min(start_index + page_size, total_scans)
    paginated_scans = scans[start_index:end_index] if total_scans > 0 else []
    
    # Process basic statistics for display
    device_stats = {}
    os_stats = {}
    hourly_counts = [0] * 24
    
    for scan in scans:
        # Use localized timestamp for hour calculation
        localized_time = scan.localized_timestamp
        if localized_time:
            hour = localized_time.hour
        else:
            hour = scan.timestamp.hour if scan.timestamp else 0
        
        # Process hourly data
        if 0 <= hour < 24:
            hourly_counts[hour] += 1
        
        # Extract device and OS info
        device = "Unknown"
        operating_system = "Unknown"
        
        if scan.user_agent:
            # Device detection
            if "Mobile" in scan.user_agent and "Tablet" not in scan.user_agent:
                device = "Mobile"
            elif "Tablet" in scan.user_agent:
                device = "Tablet"
            else:
                device = "Desktop"
                
            # OS detection
            if "Windows" in scan.user_agent:
                operating_system = "Windows"
            elif "Mac OS" in scan.user_agent or "MacOS" in scan.user_agent:
                operating_system = "macOS"
            elif "Android" in scan.user_agent:
                operating_system = "Android"
            elif "iPhone" in scan.user_agent or "iPad" in scan.user_agent or "iOS" in scan.user_agent:
                operating_system = "iOS"
            elif "Linux" in scan.user_agent and "Android" not in scan.user_agent:
                operating_system = "Linux"
                
        device_stats[device] = device_stats.get(device, 0) + 1
        os_stats[operating_system] = os_stats.get(operating_system, 0) + 1
    
    # Convert stats to list format for template
    device_data = [{"device": device, "scans": count} for device, count in device_stats.items() if count > 0]
    os_data = [{"os": os_name, "scans": count} for os_name, count in os_stats.items() if count > 0]
    
    # Calculate peak hour
    peak_hour_index = hourly_counts.index(max(hourly_counts)) if max(hourly_counts) > 0 else 0
    hour_format = peak_hour_index % 12 or 12
    ampm = 'AM' if peak_hour_index < 12 else 'PM'
    peak_hour_formatted = f"{hour_format}{ampm}"
    
    return render_template('analytics.html', 
                         qr_code=qr_code, 
                         scans=scans,
                         paginated_scans=paginated_scans,
                         total_scans=total_scans,
                         page_num=page_num,
                         total_pages=total_pages,
                         device_data=device_data,
                         os_data=os_data,
                         has_data=(total_scans > 0),
                         analytics_used=active_subscription.analytics_used,
                         analytics_limit=active_subscription.effective_analytics_limit,
                         analytics_remaining=max(0, active_subscription.effective_analytics_limit - active_subscription.analytics_used),
                         peak_hour=peak_hour_formatted,
                         user_timezone=user_timezone)


@app.route('/user/analytics')
@login_required
def user_analytics():
    # Get all QR codes for the current user
    qr_codes = QRCode.query.filter_by(user_id=current_user.id).all()
    
    # Initialize analytics data structures
    total_scans = 0
    scan_timeline = {}  # Date -> count
    qr_performance = {}  # QR ID -> scan count
    dynamic_qr_performance = {}  # Only dynamic QR codes
    device_data = {"Mobile": 0, "Desktop": 0, "Tablet": 0, "Unknown": 0}
    os_data = {"Windows": 0, "Android": 0, "iOS": 0, "macOS": 0, "Linux": 0, "Unknown": 0}
    location_data = {}
    hourly_data = [0] * 24  # Hour -> count
    
    # Get user's timezone preference
    user_timezone = session.get('user_timezone', app.config.get('DEFAULT_TIMEZONE', 'UTC'))
    
    # Process all scans
    for qr_code in qr_codes:
        qr_id = qr_code.unique_id
        qr_name = qr_code.name
        scan_count = 0
        
        # Get scans for this QR code
        scans = Scan.query.filter_by(qr_code_id=qr_code.id).all()
        for scan in scans:
            total_scans += 1
            scan_count += 1
            
            # Convert timestamp to user's local timezone using the helper function
            localized_time = get_localized_datetime(scan.timestamp, user_timezone)
            
            if localized_time:
                scan_date = localized_time.strftime('%Y-%m-%d')
                hour = localized_time.hour
            else:
                # Fallback to UTC if timezone conversion fails
                if scan.timestamp:
                    scan_date = scan.timestamp.strftime('%Y-%m-%d')
                    hour = scan.timestamp.hour
                else:
                    scan_date = 'Unknown'
                    hour = 0
            
            # Process timeline data
            if scan_date != 'Unknown':
                scan_timeline[scan_date] = scan_timeline.get(scan_date, 0) + 1
            
            # Process hourly data (ensure hour is in valid range)
            if 0 <= hour < 24:
                hourly_data[hour] += 1
            
            # Process device data
            device = "Unknown"
            operating_system = "Unknown"
            
            if scan.user_agent:
                # Device detection
                if "Mobile" in scan.user_agent and "Tablet" not in scan.user_agent:
                    device = "Mobile"
                elif "Tablet" in scan.user_agent:
                    device = "Tablet"
                else:
                    device = "Desktop"
                    
                # OS detection
                if "Windows" in scan.user_agent:
                    operating_system = "Windows"
                elif "Mac OS" in scan.user_agent or "MacOS" in scan.user_agent:
                    operating_system = "macOS"
                elif "Android" in scan.user_agent:
                    operating_system = "Android"
                elif "iPhone" in scan.user_agent or "iPad" in scan.user_agent or "iOS" in scan.user_agent:
                    operating_system = "iOS"
                elif "Linux" in scan.user_agent and "Android" not in scan.user_agent:
                    operating_system = "Linux"
                    
            device_data[device] = device_data.get(device, 0) + 1
            os_data[operating_system] = os_data.get(operating_system, 0) + 1
            
            # Process location data
            location = scan.location or "Unknown"
            location_data[location] = location_data.get(location, 0) + 1
        
        # Add to QR performance data (all QR codes)
        qr_performance[qr_id] = {
            "name": qr_name,
            "scans": scan_count,
            "type": qr_code.qr_type,
            "id": qr_id,
            "created": qr_code.created_at.strftime('%b %d, %Y') if qr_code.created_at else None
        }
        
        # Add to dynamic QR performance data (only dynamic QR codes)
        # Option 1: If you have an is_dynamic field
        if hasattr(qr_code, 'is_dynamic') and qr_code.is_dynamic:
            dynamic_qr_performance[qr_id] = {
                "name": qr_name,
                "scans": scan_count,
                "type": qr_code.qr_type,
                "id": qr_id,
                "created": qr_code.created_at.strftime('%b %d, %Y') if qr_code.created_at else None
            }
        
        # Option 2: If dynamic QR codes have a specific type (uncomment if needed)
        # if qr_code.qr_type == 'dynamic':
        #     dynamic_qr_performance[qr_id] = {
        #         "name": qr_name,
        #         "scans": scan_count,
        #         "type": qr_code.qr_type,
        #         "id": qr_id,
        #         "created": qr_code.created_at.strftime('%b %d, %Y') if qr_code.created_at else None
        #     }
        
        # Option 3: If you have a different field name (update accordingly)
        # if hasattr(qr_code, 'dynamic') and qr_code.dynamic:
        #     dynamic_qr_performance[qr_id] = {
        #         "name": qr_name,
        #         "scans": scan_count,
        #         "type": qr_code.qr_type,
        #         "id": qr_id,
        #         "created": qr_code.created_at.strftime('%b %d, %Y') if qr_code.created_at else None
        #     }
    
    # Prepare data for charts
    device_chart_data = [{"device": device, "scans": count} for device, count in device_data.items() if count > 0]
    os_chart_data = [{"os": os, "scans": count} for os, count in os_data.items() if count > 0]
    sorted_locations = sorted(location_data.items(), key=lambda x: x[1], reverse=True)[:10]
    location_chart_data = [{"location": loc, "scans": count} for loc, count in sorted_locations]
    
    # Prepare dynamic QR performance data (filtered for dynamic QR codes only)
    dynamic_qr_chart_data = list(dynamic_qr_performance.values())
    dynamic_qr_chart_data.sort(key=lambda x: x["scans"], reverse=True)
    
    # Get top 5 dynamic QR codes for display
    top_qr_codes = dynamic_qr_chart_data[:5] if dynamic_qr_chart_data else []
    
    # Calculate peak hour in user-friendly format (in user's timezone)
    peak_hour_index = hourly_data.index(max(hourly_data)) if max(hourly_data) > 0 else 0
    hour_format = peak_hour_index % 12 or 12
    ampm = 'AM' if peak_hour_index < 12 else 'PM'
    peak_hour_formatted = f"{hour_format}{ampm}"
    
    # Prepare timeline data for charts
    sorted_timeline = sorted(scan_timeline.items())
    timeline_chart_data = [{"date": date, "scans": count} for date, count in sorted_timeline]
    
    return render_template(
        'user_analytics.html',
        qr_codes=qr_codes,
        total_scans=total_scans,
        scan_timeline=scan_timeline,
        timeline_data=timeline_chart_data,
        device_data=device_chart_data,
        os_data=os_chart_data,
        hourly_data=hourly_data,
        qr_performance=dynamic_qr_chart_data,  # Pass dynamic QR data instead of all QR data
        top_qr_codes=top_qr_codes,
        location_data=location_chart_data,
        user_timezone=user_timezone,
        peak_hour=peak_hour_formatted
    )
@app.route('/qr/<qr_id>/export/<format>')
@login_required
def export_qr(qr_id, format):
    qr_code = QRCode.query.filter_by(unique_id=qr_id).first_or_404()
    
    # Ensure the QR code belongs to the current user
    if qr_code.user_id != current_user.id:
        flash('You do not have permission to export this QR code.')
        return redirect(url_for('dashboard'))
    
    # Only allow PNG format
    if format != 'png':
        flash('Only PNG format is supported for export.')
        return redirect(url_for('view_qr', qr_id=qr_id))
    
    # Temporarily set the export format
    original_format = qr_code.export_type
    qr_code.export_type = format
    
    # Generate QR code image
    qr_image, _ = generate_qr_code(qr_code)
    
    # Reset to original format
    qr_code.export_type = original_format
    
    # Determine mime type - only PNG is supported
    mime_type = 'image/png'
    
    # If base64 data URL, convert back to binary
    if qr_image.startswith('data:'):
        # Extract just the base64 part
        header, encoded = qr_image.split(",", 1)
        img_data = base64.b64decode(encoded)
        buffer = BytesIO(img_data)
    else:
        buffer = BytesIO(base64.b64decode(qr_image))
    
    # Return the image as a file download
    return send_file(buffer, mimetype=mime_type, as_attachment=True, download_name=f"{qr_code.name}.{format}")\
    
# ==================================================================
# COMPLETE REDIRECT_QR FUNCTION - Replace your existing one
# ==================================================================

@app.route('/r/<qr_id>')
def redirect_qr(qr_id):
    from flask import render_template, jsonify
    import traceback
    
    try:
        # Step 1: Retrieve the QR code record
        qr_code = QRCode.query.filter_by(unique_id=qr_id).first_or_404()
        
        # Step 2: Check scan limits for the QR code owner
        user_id = qr_code.user_id
        
        # Get user's active subscription
        active_subscription = (
            SubscribedUser.query
            .filter(SubscribedUser.U_ID == user_id)
            .filter(SubscribedUser.end_date > datetime.now(UTC))
            .filter(SubscribedUser._is_active == True)
            .first()
        )
        
        # Check if scan limit reached
        if active_subscription and active_subscription.subscription.scan_limit > 0:
            if not active_subscription.can_scan():
                # Get user information for display
                owner = User.query.get(user_id)
                owner_name = owner.name if owner else "QR Code Owner"

                # Render scan limit reached page
                return render_template('scan_limit_reached.html',
                                     qr_code=qr_code,
                                     owner_name=owner_name)
        
        # Step 3: Record scan (for both dynamic and static QR codes)
        # Check for duplicate scan from same IP within last 10 seconds
        ten_seconds_ago = datetime.now(UTC) - timedelta(seconds=10)
        recent_scan = Scan.query.filter(
            Scan.qr_code_id == qr_code.id,
            Scan.ip_address == request.remote_addr,
            Scan.timestamp >= ten_seconds_ago
        ).first()

        # Only record scan if no recent duplicate found
        if not recent_scan:
            scan = Scan(
                qr_code_id=qr_code.id,
                ip_address=request.remote_addr,
                user_agent=request.user_agent.string,
                location=request.headers.get('X-Forwarded-For', request.remote_addr)
            )
            db.session.add(scan)

            # Step 4: Increment scan count for subscription
            if active_subscription and active_subscription.subscription.scan_limit > 0:
                active_subscription.increment_scan()
                app.logger.info(f"Scan recorded for user {user_id}. Scans used: {active_subscription.scans_used}/{active_subscription.subscription.scan_limit}")

            db.session.commit()
        else:
            app.logger.info(f"Duplicate scan detected for QR {qr_id} from IP {request.remote_addr} within 10 seconds - not recording")
        
        # Step 5: Handle QR code redirection based on type
        qr_type = qr_code.qr_type
        content = json.loads(qr_code.content) if qr_code.content else {}
        # Step 2: Retrieve specific model data based on QR type
        detail = None
        
        if qr_type == 'wifi':
            detail = qr_code.wifi_detail
            if not detail and content:
                app.logger.info(f"No WiFi detail found in database, creating from content: {content}")
                detail = type('WifiDetail', (), {
                    'ssid': content.get('ssid', 'Unknown Network'),
                    'password': content.get('password', ''),
                    'encryption': content.get('encryption', 'WPA')
                })()
            
            if not detail:
                app.logger.warning(f"No WiFi data found for QR {qr_id}, using defaults")
                detail = type('WifiDetail', (), {
                    'ssid': 'Unknown Network',
                    'password': '',
                    'encryption': 'WPA'
                })()
            
            return render_template('wifi_display.html', detail=detail)
            
        elif qr_type == 'vcard':
            detail = qr_code.vcard_detail
            if not detail and content:
                social_media_data = content.get('social_media', None)
                if isinstance(social_media_data, str):
                    try:
                        social_media_data = json.loads(social_media_data)
                    except (json.JSONDecodeError, TypeError):
                        social_media_data = None
                
                detail = type('VCardDetail', (), {
                    'name': content.get('name', ''),
                    'phone': content.get('phone', ''),
                    'email': content.get('email', ''),
                    'company': content.get('company', ''),
                    'title': content.get('title', ''),
                    'address': content.get('address', ''),
                    'website': content.get('website', ''),
                    'logo_path': content.get('logo_path', ''),
                    'primary_color': content.get('primary_color', '#3366CC'),
                    'secondary_color': content.get('secondary_color', '#5588EE'),
                    'social_media': social_media_data if isinstance(social_media_data, dict) else json.dumps(social_media_data) if social_media_data else None
                })()
            
            return render_template('vcard_display.html', detail=detail)
        elif qr_type == 'event':
            # Use the direct relationship from the QR code to its detail
            detail = qr_code.event_detail
            # Only fall back to JSON content if no database record exists
            if not detail and content:
                app.logger.info(f"No event detail found in database, creating from content: {content}")
                detail = type('EventDetail', (), {
                    'title': content.get('title', ''),
                    'organizer': content.get('organizer', ''),
                    'start_date': content.get('start_date', ''),
                    'end_time': content.get('end_time', ''),
                    'location': content.get('location', ''),
                    'description': content.get('description', '')
                })()
            return render_template('event_display.html', detail=detail)

        elif qr_type == 'image':
            detail = qr_code.image_detail
            if not detail and content:
                detail = type('ImageDetail', (), {
                    'image_path': content.get('image_path', ''),
                    'caption': content.get('caption', ''),
                    'bg_color_1': content.get('bg_color_1', '#0a0a0a'),
                    'bg_color_2': content.get('bg_color_2', '#1a1a2e'),
                    'bg_direction': content.get('bg_direction', 'to bottom')
                })()
            if detail:
                return render_template('image_display.html', detail=detail)
            else:
                return render_template('error.html', message="Image information not found")

        elif qr_type == 'text':
            # Use the direct relationship from the QR code to its detail
            detail = qr_code.text_detail
            # Only fall back to JSON content if no database record exists
            if not detail and content:
                app.logger.info(f"No text detail found in database, creating from content: {content}")
                detail = type('TextDetail', (), {
                    'text': content.get('text', '')
                })()
            return render_template('text_display.html', detail=detail, now=datetime.now().strftime('%d %b %Y, %H:%M'))

        elif qr_type == 'link':
            # Link types redirect directly, no template needed
            url = None
            if hasattr(qr_code, 'link_detail') and qr_code.link_detail:
                url = qr_code.link_detail.url
            elif 'url' in content:
                url = content['url']
            
            if url:
                return redirect(url)
            else:
                return render_template('error.html', message="Link information not found")
                
        elif qr_type == 'email':
            # Email types - show email options page
            if hasattr(qr_code, 'email_detail') and qr_code.email_detail:
                email_data = {
                    'email': qr_code.email_detail.email,
                    'subject': qr_code.email_detail.subject or '',
                    'body': qr_code.email_detail.body or ''
                }
                return render_template('email_display.html', detail=email_data)
            elif 'email' in content:
                email_data = {
                    'email': content['email'],
                    'subject': content.get('subject', ''),
                    'body': content.get('body', '')
                }
                return render_template('email_display.html', detail=email_data)
            else:
                return render_template('error.html', message="Email information not found")
                
        elif qr_type == 'call':
            # Call types - show call options page
            if hasattr(qr_code, 'phone_detail') and qr_code.phone_detail:
                call_data = {
                    'phone': qr_code.phone_detail.phone
                }
                return render_template('call_display.html', detail=call_data)
            elif 'phone' in content:
                call_data = {
                    'phone': content['phone']
                }
                return render_template('call_display.html', detail=call_data)
            else:
                return render_template('error.html', message="Phone information not found")
                
        elif qr_type == 'sms':
            # SMS types - show sms options page
            if hasattr(qr_code, 'sms_detail') and qr_code.sms_detail:
                sms_data = {
                    'phone': qr_code.sms_detail.phone,
                    'message': qr_code.sms_detail.message or ''
                }
                return render_template('sms_display.html', detail=sms_data)
            elif 'phone' in content:
                sms_data = {
                    'phone': content['phone'],
                    'message': content.get('message', '')
                }
                return render_template('sms_display.html', detail=sms_data)
            else:
                return render_template('error.html', message="SMS information not found")
                
        elif qr_type == 'whatsapp':
            # WhatsApp types redirect to wa.me
            if hasattr(qr_code, 'whatsapp_detail') and qr_code.whatsapp_detail:
                # Clean phone number (remove non-digits)
                phone = ''.join(c for c in qr_code.whatsapp_detail.phone if c.isdigit())
                return redirect(f"https://wa.me/{phone}?text={qr_code.whatsapp_detail.message or ''}")
            elif 'phone' in content:
                phone = ''.join(c for c in content['phone'] if c.isdigit())
                return redirect(f"https://wa.me/{phone}?text={content.get('message', '')}")
            else:
                return render_template('error.html', message="WhatsApp information not found")
        
        # If no specific handling is found, show an error or default page
        return render_template('error.html', message="Unable to process QR code")
        
    except Exception as e:
        app.logger.error(f"Unhandled error in redirect_qr for {qr_id}: {e}")
        app.logger.error(traceback.format_exc())
        return render_template('error.html', message=f"An error occurred: {str(e)}")



@app.route('/batch-export', methods=['POST'])
@login_required
def batch_export():
    user_id = current_user.id
    
    # Check if user has an active subscription
    active_subscription = (
        SubscribedUser.query
        .filter(SubscribedUser.U_ID == user_id)
        .filter(SubscribedUser.end_date > datetime.now(UTC))
        .filter(SubscribedUser._is_active == True)
        .first()
    )
    
    if not active_subscription:
        flash('You need an active subscription to export QR codes in batch.', 'warning')
        return redirect(url_for('subscription.user_subscriptions'))
    
    # Check if batch export is allowed for this subscription tier
    if active_subscription.subscription.tier < 2:  # Adjust tier requirement as needed
        flash('Batch export is only available for higher tier subscriptions.', 'warning')
        return redirect(url_for('dashboard'))
    
    qr_ids = request.form.getlist('qr_ids')
    format = request.form.get('format', 'zip')
    
    if not qr_ids:
        flash('No QR codes selected for export.')
        return redirect(url_for('dashboard'))
    
    # Get QR codes and ensure they belong to the current user
    data_list = []
    for qr_id in qr_ids:
        qr_code = QRCode.query.filter_by(unique_id=qr_id).first()
        if qr_code and qr_code.user_id == user_id:
            # Generate QR data
            qr_data = generate_qr_data(qr_code)
            
            # Get options for the QR code
            options = get_qr_options(qr_code)
            
            data_list.append({
                'data': qr_data,
                'options': options,
                'label': qr_code.name
            })
    
    if not data_list:
        flash('No valid QR codes selected for export.')
        return redirect(url_for('dashboard'))
    
    # Generate batch export
    result = batch_generate_qr(data_list, output_format=format)
    
    # Record usage
    if active_subscription:
        # Consider this a single analytics usage
        if active_subscription.analytics_used < active_subscription.effective_analytics_limit:
            active_subscription.analytics_used += 1
            db.session.commit()
    
    # Determine mime type
    mime_type = "application/zip" if format == "zip" else "application/pdf"
    
    # If base64 data URL, convert back to binary
    if result.startswith('data:'):
        # Extract just the base64 part
        header, encoded = result.split(",", 1)
        file_data = base64.b64decode(encoded)
        buffer = BytesIO(file_data)
    else:
        buffer = BytesIO(base64.b64decode(result))
    
    # Return the file as a download
    filename = f"qr_batch_export.{format}"
    return send_file(buffer, mimetype=mime_type, as_attachment=True, download_name=filename)

@app.route('/help')
def help_center():
    """Help center page with guides and tutorials for using QR Dada"""
    return render_template('help_center.html')

# Service Pages Routes
@app.route('/services/qr-code-for-url')
def service_qr_url():
    """QR Code for URL/Links service page"""
    return render_template('services/qr_url.html')

@app.route('/services/qr-code-for-email')
def service_qr_email():
    """QR Code for Email service page"""
    return render_template('services/qr_email.html')

@app.route('/services/qr-code-for-wifi')
def service_qr_wifi():
    """QR Code for WiFi service page"""
    return render_template('services/qr_wifi.html')

@app.route('/services/qr-code-for-sms')
def service_qr_sms():
    """QR Code for SMS service page"""
    return render_template('services/qr_sms.html')

@app.route('/services/qr-code-for-event')
def service_qr_event():
    """QR Code for Event service page"""
    return render_template('services/qr_event.html')

@app.route('/services/qr-code-for-image')
def service_qr_image():
    """QR Code for Image service page"""
    return render_template('services/qr_image.html')

@app.route('/services/qr-code-for-call')
def service_qr_call():
    """QR Code for Phone Call service page"""
    return render_template('services/qr_call.html')

@app.route('/services/qr-code-for-whatsapp')
def service_qr_whatsapp():
    """QR Code for WhatsApp service page"""
    return render_template('services/qr_whatsapp.html')

@app.route('/services/qr-code-for-vcard')
def service_qr_vcard():
    """QR Code for vCard service page"""
    return render_template('services/qr_vcard.html')

@app.route('/services/qr-code-for-text')
def service_qr_text():
    """QR Code for Text service page"""
    return render_template('services/qr_text.html')

@app.route('/qr-code-scanner')
def qr_scanner():
    """Free QR Code Scanner tool"""
    return render_template('qr_scanner.html')

@app.route('/api/scan-qr', methods=['POST'])
def api_scan_qr():
    """Server-side QR code scanning using OpenCV for styled/colored QR codes"""
    if 'image' not in request.files:
        return jsonify({'success': False, 'error': 'No image provided'}), 400

    file = request.files['image']
    if not file.filename:
        return jsonify({'success': False, 'error': 'No file selected'}), 400

    try:
        import cv2
        import numpy as np

        # Read image bytes
        img_bytes = file.read()
        nparr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if img is None:
            return jsonify({'success': False, 'error': 'Invalid image'}), 400

        decoded_data = None
        h, w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        detector = cv2.QRCodeDetector()

        # Helper: try OpenCV QR detector
        def try_cv_detect(image):
            try:
                val, _, _ = detector.detectAndDecode(image)
                return val if val else None
            except Exception:
                return None

        # Helper: try pyzbar if available
        def try_pyzbar(image):
            try:
                from pyzbar.pyzbar import decode as pyzbar_decode
                results = pyzbar_decode(image)
                if results:
                    return results[0].data.decode('utf-8', errors='replace')
            except Exception:
                pass
            return None

        # Strategy 1: OpenCV on original
        decoded_data = try_cv_detect(img)

        # Strategy 2: OpenCV on grayscale
        if not decoded_data:
            decoded_data = try_cv_detect(gray)

        # Strategy 3: Otsu threshold
        if not decoded_data:
            _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            decoded_data = try_cv_detect(binary)

        # Strategy 4: Color-to-black (for colored QR codes - orange, blue, etc.)
        if not decoded_data:
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            white_mask = cv2.inRange(hsv, np.array([0, 0, 180]), np.array([180, 60, 255]))
            color_bw = np.full_like(gray, 0)
            color_bw[white_mask > 0] = 255
            decoded_data = try_cv_detect(color_bw)

        # Strategy 5: Gaussian blur + Otsu (smooths circular dots)
        if not decoded_data:
            blurred = cv2.GaussianBlur(gray, (7, 7), 0)
            _, blur_bin = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            decoded_data = try_cv_detect(blur_bin)

        # Strategy 6: Morphological close (fills circular patterns into solid areas)
        if not decoded_data:
            _, thresh = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)
            kernel_size = max(3, min(h, w) // 80)
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
            closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
            inverted = cv2.bitwise_not(closed)
            decoded_data = try_cv_detect(inverted)

        # Strategy 7: Scale up small images
        if not decoded_data and max(h, w) < 800:
            scale = 1200 / max(h, w)
            resized = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            decoded_data = try_cv_detect(resized)

        # Strategy 8: Adaptive threshold
        if not decoded_data:
            block_size = max(51, (min(h, w) // 10) | 1)
            adaptive = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block_size, 10)
            decoded_data = try_cv_detect(adaptive)

        # Strategy 9: Scale up + color-to-black
        if not decoded_data:
            scale = max(2.0, 1500 / max(h, w))
            big = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            big_hsv = cv2.cvtColor(big, cv2.COLOR_BGR2HSV)
            big_white = cv2.inRange(big_hsv, np.array([0, 0, 180]), np.array([180, 60, 255]))
            big_bw = np.full((big.shape[0], big.shape[1]), 0, dtype=np.uint8)
            big_bw[big_white > 0] = 255
            decoded_data = try_cv_detect(big_bw)

        # Strategy 10: Try pyzbar as fallback (works on Linux/EC2)
        if not decoded_data:
            decoded_data = try_pyzbar(img)
        if not decoded_data:
            decoded_data = try_pyzbar(gray)
        if not decoded_data:
            decoded_data = try_pyzbar(binary)

        if decoded_data:
            return jsonify({'success': True, 'data': decoded_data})
        else:
            return jsonify({'success': False, 'error': 'No QR code found'})

    except Exception as e:
        logging.error(f"QR scan error: {str(e)}")
        return jsonify({'success': False, 'error': 'Scan failed'}), 500

def apply_watermark(qr_img, text):
    """Apply text watermark to QR code"""
    # Create a transparent layer for watermark
    watermark = Image.new('RGBA', qr_img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(watermark)
    
    # Set watermark color and opacity
    watermark_color = (0, 0, 0, 60)  # Black with 60/255 opacity
    
    # Try to load font, fall back to default
    try:
        font_size = min(qr_img.size) // 15
        font = ImageFont.truetype("arial.ttf", font_size)
    except IOError:
        font = ImageFont.load_default()
    
    # Calculate text position (bottom right corner)
    text_width = draw.textlength(text, font=font)
    text_position = (qr_img.size[0] - text_width - 10, qr_img.size[1] - font_size - 10)
    
    # Draw text
    draw.text(text_position, text, fill=watermark_color, font=font)
    
    # Composite watermark with QR code
    result = Image.alpha_composite(qr_img.convert('RGBA'), watermark)
    
    return result

def apply_frame(qr_img, frame_type, options):
    """Apply frame around QR code with black frame color only"""
    if not frame_type:  # Added check for empty frame_type
        return qr_img
        
    qr_width, qr_height = qr_img.size
    
    # Determine frame size
    frame_padding = qr_width // 10
    frame_width = qr_width + (frame_padding * 2)
    
    # Additional height for text
    text_height = 0
    if frame_type in ['scan_me', 'branded']:
        text_height = frame_padding * 2
    
    frame_height = qr_height + (frame_padding * 2) + text_height
    
    # Get background color from options
    background_color = options.get('background_color', '#FFFFFF')
    
    # Convert background color to RGB if needed - Add validation
    try:
        if isinstance(background_color, str) and background_color.startswith('#'):
            # Validate hex color format
            if not all(c in '0123456789ABCDEFabcdef' for c in background_color.lstrip('#')):
                background_color = '#FFFFFF'  # Default to white if invalid
            bg_color_rgb = tuple(int(background_color.lstrip('#')[i:i+2], 16) for i in (0, 2, 4)) + (255,)
        else:
            bg_color_rgb = background_color + (255,) if len(background_color) == 3 else background_color
    except Exception:
        # Fallback to white if there's any error
        bg_color_rgb = (255, 255, 255, 255)
    
    # FORCE frame color to be black, ignore any other settings
    frame_color_rgb = (0, 0, 0, 255)  # Black with full opacity
    
    # Debug output
    print(f"Applying frame with BLACK color: {frame_color_rgb}, ignoring other colors")
    
    # Special handling for circle frame
    if frame_type == 'circle':
        # For circle, ensure the circle is large enough to contain the QR code with proper padding
        # Use a larger padding for circular frames to ensure the QR code fits well
        circle_padding = int(frame_padding * 1.5)
        circle_diameter = max(qr_width, qr_height) + (circle_padding * 2)
        
        # Create a new image with the circle diameter
        circle_img = Image.new('RGBA', (circle_diameter, circle_diameter), bg_color_rgb)
        circle_draw = ImageDraw.Draw(circle_img)
        
        # Draw the circle frame - make the outline thicker for better visibility
        circle_line_width = max(2, circle_padding // 3)
        circle_draw.ellipse([0, 0, circle_diameter-1, circle_diameter-1], 
                           outline=frame_color_rgb, 
                           width=circle_line_width)
        
        # Position the QR code in the center of the circle
        qr_pos = ((circle_diameter - qr_width) // 2, (circle_diameter - qr_height) // 2)
        circle_img.paste(qr_img, qr_pos)
        
        return circle_img
    
    # For other frame types, create standard frame image
    frame_img = Image.new('RGBA', (frame_width, frame_height), bg_color_rgb)
    draw = ImageDraw.Draw(frame_img)
    
    # Draw other frame types
    if frame_type == 'square':
        draw.rectangle([0, 0, frame_width-1, frame_height-1], outline=frame_color_rgb, width=max(1, frame_padding // 2))
    elif frame_type == 'rounded':
        radius = frame_padding
        # Draw rounded rectangle manually since older Pillow versions might not have rounded_rectangle
        # Left and right vertical lines
        draw.line([(0, radius), (0, frame_height - radius - 1)], fill=frame_color_rgb, width=max(1, frame_padding // 2))
        draw.line([(frame_width - 1, radius), (frame_width - 1, frame_height - radius - 1)], fill=frame_color_rgb, width=max(1, frame_padding // 2))
        # Top and bottom horizontal lines
        draw.line([(radius, 0), (frame_width - radius - 1, 0)], fill=frame_color_rgb, width=max(1, frame_padding // 2))
        draw.line([(radius, frame_height - 1), (frame_width - radius - 1, frame_height - 1)], fill=frame_color_rgb, width=max(1, frame_padding // 2))
       
        # Four arc corners
        draw.arc([(0, 0), (radius * 2, radius * 2)], 180, 270, fill=frame_color_rgb, width=max(1, frame_padding // 2))
        draw.arc([(frame_width - radius * 2 - 1, 0), (frame_width - 1, radius * 2)], 270, 0, fill=frame_color_rgb, width=max(1, frame_padding // 2))
        draw.arc([(0, frame_height - radius * 2 - 1), (radius * 2, frame_height - 1)], 90, 180, fill=frame_color_rgb, width=max(1, frame_padding // 2))
        draw.arc([(frame_width - radius * 2 - 1, frame_height - radius * 2 - 1), (frame_width - 1, frame_height - 1)], 0, 90, fill=frame_color_rgb, width=max(1, frame_padding // 2))
    elif frame_type == 'scan_me':
        # Create a black bar at the top with text
        bar_height = text_height
        draw.rectangle([0, 0, frame_width, bar_height], fill=frame_color_rgb)
        
        # Draw rectangle frame
        draw.rectangle([0, 0, frame_width-1, frame_height-1], outline=frame_color_rgb, width=max(1, frame_padding // 2))
        
        # Add "Scan Me" text
        try:
            font = ImageFont.truetype("arial.ttf", frame_padding)
        except IOError:
            # Fallback to default font
            font = ImageFont.load_default()
        
        text = options.get('frame_text', 'SCAN ME')
        try:
            text_width = draw.textlength(text, font=font)
        except AttributeError:
            # Fallback for older Pillow versions
            text_width = font.getsize(text)[0]
            
        text_position = ((frame_width - text_width) // 2, (bar_height - frame_padding) // 2)
        draw.text(text_position, text, fill=(255, 255, 255, 255), font=font)  # White text
    elif frame_type == 'branded':
        # Create branded frame with company name
        # Black bar at the bottom with text
        bar_height = text_height
        draw.rectangle([0, 0, frame_width, bar_height], fill=frame_color_rgb)
        
        # Draw the outer frame
        draw.rectangle([0, 0, frame_width-1, frame_height-1], outline=frame_color_rgb, width=max(1, frame_padding // 2))
        
        # Add company name
        company_name = options.get('frame_text', 'COMPANY')
        try:
            font = ImageFont.truetype("arial.ttf", frame_padding)
        except IOError:
            font = ImageFont.load_default()
        
        try:
            text_width = draw.textlength(company_name, font=font)
        except AttributeError:
            # Fallback for older Pillow versions
            text_width = font.getsize(company_name)[0]
            
        text_position = ((frame_width - text_width) // 2, (bar_height - frame_padding) // 2)
        draw.text(text_position, company_name, fill=(255, 255, 255, 255), font=font)  # White text
    
    # Paste QR code onto frame
    qr_position = ((frame_width - qr_width) // 2, (frame_height - qr_height) // 2)
    if frame_type in ['scan_me', 'branded']:
        qr_position = ((frame_width - qr_width) // 2, text_height + frame_padding)
    
    frame_img.paste(qr_img, qr_position)
    
    return frame_img


# ==================================================================
# COMPLETE GENERATE_QR_DATA FUNCTION - Replace your existing one
# ==================================================================

def fold_icalendar_line(line, max_length=75):
    """Fold long iCalendar lines according to RFC 5545.
    Lines longer than max_length characters should be split with continuation."""
    if len(line) <= max_length:
        return line

    # Split the line into chunks of max_length
    result = []
    while line:
        if len(line) <= max_length:
            result.append(line)
            break
        # Find a good break point (prefer to break at a space if possible)
        break_point = max_length
        result.append(line[:break_point])
        line = ' ' + line[break_point:]  # Indent continuation with space

    return '\r\n'.join(result)

def generate_qr_data(qr_code):
    """Generate QR data string based on QR code type and content from detailed tables"""
    try:
        qr_type = qr_code.qr_type
        
        # Generate QR data based on type
        qr_data = ""
        
        # For dynamic QR codes, always use the redirect URL regardless of type
        if qr_code.is_dynamic:
            site_url = os.getenv('SITE_URL', '').rstrip('/')
            if site_url:
                qr_data = f"{site_url}/r/{qr_code.unique_id}"
            else:
                qr_data = url_for('redirect_qr', qr_id=qr_code.unique_id, _external=True)
            return qr_data
            
        # For static QR codes, use the appropriate data format by type
        if qr_type == 'link':
            # Try to get data from link_detail table first
            if hasattr(qr_code, 'link_detail') and qr_code.link_detail:
                qr_data = qr_code.link_detail.url
            else:
                # Fallback to JSON content for backward compatibility
                content = json.loads(qr_code.content)
                qr_data = content.get('url', 'https://example.com')
                
        elif qr_type == 'email':
            # Try to get data from email_detail table first
            if hasattr(qr_code, 'email_detail') and qr_code.email_detail:
                email = qr_code.email_detail.email
                subject = qr_code.email_detail.subject or ''
                body = qr_code.email_detail.body or ''
            else:
                # Fallback to JSON content
                content = json.loads(qr_code.content)
                email = content.get('email', '')
                subject = content.get('subject', '')
                body = content.get('body', '')
                
            # Format mailto URL
            qr_data = f"mailto:{email}?subject={subject}&body={body}"
            
        elif qr_type == 'text':
            # Try to get data from text_detail table first
            if hasattr(qr_code, 'text_detail') and qr_code.text_detail:
                qr_data = qr_code.text_detail.text
            else:
                # Fallback to JSON content
                content = json.loads(qr_code.content)
                qr_data = content.get('text', '')
                
        elif qr_type == 'call':
            # Try to get data from phone_detail table first
            if hasattr(qr_code, 'phone_detail') and qr_code.phone_detail:
                phone_number = qr_code.phone_detail.phone
                # Ensure phone number starts with + for international format
                if phone_number and not phone_number.startswith('+'):
                    phone_number = '+' + phone_number
                # URL-encode the + as %2B for better compatibility with Google Scanner
                phone_number = phone_number.replace('+', '%2B')
                qr_data = f"tel:{phone_number}"
            else:
                # Fallback to JSON content
                content = json.loads(qr_code.content)
                phone_number = content.get('phone', '')
                # Ensure phone number starts with + for international format
                if phone_number and not phone_number.startswith('+'):
                    phone_number = '+' + phone_number
                # URL-encode the + as %2B for better compatibility with Google Scanner
                phone_number = phone_number.replace('+', '%2B')
                qr_data = f"tel:{phone_number}"
                
        elif qr_type == 'sms':
            # Try to get data from sms_detail table first
            if hasattr(qr_code, 'sms_detail') and qr_code.sms_detail:
                phone = qr_code.sms_detail.phone
                message = qr_code.sms_detail.message or ''
            else:
                # Fallback to JSON content
                content = json.loads(qr_code.content)
                phone = content.get('phone', '')
                message = content.get('message', '')

            # Ensure phone number starts with + for international format
            if phone and not phone.startswith('+'):
                phone = '+' + phone

            # URL-encode the + as %2B for better compatibility with Google Scanner
            phone = phone.replace('+', '%2B')

            # Format SMS URL
            qr_data = f"sms:{phone}?body={message}"
            
        elif qr_type == 'whatsapp':
            # Try to get data from whatsapp_detail table first
            if hasattr(qr_code, 'whatsapp_detail') and qr_code.whatsapp_detail:
                phone = qr_code.whatsapp_detail.phone
                # Remove any non-numeric characters from phone number
                phone = ''.join(c for c in phone if c.isdigit())
                message = qr_code.whatsapp_detail.message or ''
            else:
                # Fallback to JSON content
                content = json.loads(qr_code.content)
                phone = content.get('phone', '')
                # Remove any non-numeric characters
                phone = ''.join(c for c in phone if c.isdigit())
                message = content.get('message', '')
                
            # Format WhatsApp URL
            qr_data = f"https://wa.me/{phone}?text={message}"
            
        elif qr_type == 'wifi':
            # *** COMPLETE WIFI QR DATA GENERATION ***
            # Try to get data from wifi_detail table first
            if hasattr(qr_code, 'wifi_detail') and qr_code.wifi_detail:
                ssid = qr_code.wifi_detail.ssid
                password = qr_code.wifi_detail.password or ''
                encryption = qr_code.wifi_detail.encryption or 'WPA'
                
                app.logger.info(f"WiFi QR from DB - SSID: {ssid}, Encryption: {encryption}")
            else:
                # Fallback to JSON content
                content = json.loads(qr_code.content) if qr_code.content else {}
                ssid = content.get('ssid', 'Unknown Network')
                password = content.get('password', '')
                encryption = content.get('encryption', 'WPA')
                
                app.logger.info(f"WiFi QR from JSON - SSID: {ssid}, Encryption: {encryption}")
                
            # Escape special characters in SSID and password
            def escape_wifi_string(s):
                if not s:
                    return ''
                # Escape semicolons, commas, backslashes, and quotes according to WiFi QR standard
                return s.replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('"', '\\"')
            
            # Format WiFi QR string according to standard
            # Format: WIFI:T:<encryption>;S:<ssid>;P:<password>;H:<hidden>;;
            ssid_escaped = escape_wifi_string(ssid)
            password_escaped = escape_wifi_string(password)
            
            qr_data = f"WIFI:T:{encryption};S:{ssid_escaped};"
            
            if password:
                qr_data += f"P:{password_escaped};"
            
            # Add hidden flag (default: false)
            qr_data += "H:false;;"
            
            app.logger.info(f"Generated WiFi QR string: WIFI:T:{encryption};S:{ssid_escaped};P:***;H:false;;")
            
        elif qr_type == 'vcard':
            # Try to get data from vcard_detail table first
            if hasattr(qr_code, 'vcard_detail') and qr_code.vcard_detail:
                detail = qr_code.vcard_detail
                name = detail.name
                phone = detail.phone or ''
                email = detail.email or ''
                company = detail.company or ''
                title = detail.title or ''
                address = detail.address or ''
                website = detail.website or ''
                
                # New fields
                logo_url = ''
                if detail.logo_path:
                    logo_url = url_for('static', filename=f'uploads/{detail.logo_path}', _external=True)
                
                primary_color = detail.primary_color or '#3366CC'
                secondary_color = detail.secondary_color or '#5588EE'
                
                social_media = {}
                if detail.social_media:
                    try:
                        social_media = json.loads(detail.social_media)
                    except:
                        social_media = {}
            else:
                # Fallback to JSON content
                content = json.loads(qr_code.content)
                name = content.get('name', '')
                phone = content.get('phone', '')
                email = content.get('email', '')
                company = content.get('company', '')
                title = content.get('title', '')
                address = content.get('address', '')
                website = content.get('website', '')
                logo_url = ''
                primary_color = '#3366CC'
                secondary_color = '#5588EE'
                social_media = {}
            
            # Create enhanced vCard data
            vcard = [
                "BEGIN:VCARD",
                "VERSION:4.0",  # Updated to version 4.0 for better feature support
                f"N:{name}",
                f"FN:{name}"
            ]
            
            if phone:
                vcard.append(f"TEL:{phone}")
                
            if email:
                vcard.append(f"EMAIL:{email}")
                
            if company:
                vcard.append(f"ORG:{company}")
                
            if title:
                vcard.append(f"TITLE:{title}")
                
            if address:
                vcard.append(f"ADR:{address}")
                
            if website:
                vcard.append(f"URL:{website}")
            
            # Add logo if available
            if logo_url:
                vcard.append(f"LOGO;TYPE=PNG:{logo_url}")
            
            # Add custom fields for colors (using X- prefix for custom fields)
            vcard.append(f"X-PRIMARY-COLOR:{primary_color}")
            vcard.append(f"X-SECONDARY-COLOR:{secondary_color}")
            
            # Add social media links
            for platform, link in social_media.items():
                vcard.append(f"X-SOCIALPROFILE;TYPE={platform.upper()}:{link}")
            
            vcard.append("END:VCARD")
            qr_data = "\n".join(vcard)
            
        elif qr_type == 'event':
            # Try to get data from event_detail table first
            if hasattr(qr_code, 'event_detail') and qr_code.event_detail:
                detail = qr_code.event_detail
                title = detail.title
                location = detail.location or ''
                start_date = detail.start_date
                end_time = detail.end_time
                description = detail.description or ''
                organizer = detail.organizer or ''
                print(f"Event QR Data - Using database detail. Description: '{description}'")

                # Format dates for iCalendar if available
                start_date_str = ''
                end_time_str = ''

                if start_date:
                    start_date_str = start_date.strftime('%Y%m%dT%H%M%S')

                if end_time:
                    end_time_str = end_time.strftime('%Y%m%dT%H%M%S')
            else:
                # Fallback to JSON content
                content = json.loads(qr_code.content)
                title = content.get('title', '')
                location = content.get('location', '')
                start_date_str = content.get('start_date', '')
                end_time_str = content.get('end_time', '')
                description = content.get('description', '')
                organizer = content.get('organizer', '')
                print(f"Event QR Data - Using JSON content. Description: '{description}'")
                
            # Create iCalendar event
            vevent = [
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "BEGIN:VEVENT"
            ]
            
            if title:
                # Escape special characters for iCalendar format (no comma escaping for SUMMARY)
                escaped_title = title.replace('\\', '\\\\').replace(';', '\\;').replace('\r\n', '\\n').replace('\n', '\\n').replace('\r', '\\n')
                title_line = fold_icalendar_line(f"SUMMARY:{escaped_title}")
                vevent.append(title_line)

            if location:
                # Escape special characters for iCalendar format (no comma escaping for LOCATION)
                escaped_location = location.replace('\\', '\\\\').replace(';', '\\;').replace('\r\n', '\\n').replace('\n', '\\n').replace('\r', '\\n')
                location_line = fold_icalendar_line(f"LOCATION:{escaped_location}")
                vevent.append(location_line)

            if start_date_str:
                vevent.append(f"DTSTART:{start_date_str}")

            if end_time_str:
                vevent.append(f"DTEND:{end_time_str}")

            if description:
                # Escape special characters for iCalendar format (no comma escaping for DESCRIPTION)
                escaped_description = description.replace('\\', '\\\\').replace(';', '\\;').replace('\r\n', '\\n').replace('\n', '\\n').replace('\r', '\\n')
                description_line = fold_icalendar_line(f"DESCRIPTION:{escaped_description}")
                vevent.append(description_line)
                print(f"Event QR Data - Description added to vevent: '{escaped_description}'")
            else:
                print("Event QR Data - No description provided")

            if organizer:
                # Escape special characters for iCalendar format (no comma escaping for ORGANIZER)
                escaped_organizer = organizer.replace('\\', '\\\\').replace(';', '\\;').replace('\r\n', '\\n').replace('\n', '\\n').replace('\r', '\\n')
                organizer_line = fold_icalendar_line(f"ORGANIZER:{escaped_organizer}")
                vevent.append(organizer_line)

            vevent.extend([
                "END:VEVENT",
                "END:VCALENDAR"
            ])

            qr_data = "\r\n".join(vevent)
            print(f"Event QR Data - Final iCalendar data:\n{qr_data}")

        elif qr_type == 'image':
            # Image QR codes must use a URL since image data can't be embedded in QR
            # Use SITE_URL from env so it works when scanned from any device
            site_url = os.getenv('SITE_URL', '').rstrip('/')
            if site_url:
                qr_data = f"{site_url}/r/{qr_code.unique_id}"
            else:
                qr_data = url_for('redirect_qr', qr_id=qr_code.unique_id, _external=True)

        return qr_data
    except Exception as e:
        app.logger.error(f"Error generating QR data: {str(e)}")
        import traceback
        app.logger.error(traceback.format_exc())
        # Return a fallback string so something is displayed
        return "https://example.com/error"
    
def create_inner_eye_mask(img):
    """Create mask for inner eyes with improved accuracy and size scaling"""
    img_size = img.size[0]
    mask = Image.new('L', img.size, 0)
    draw = ImageDraw.Draw(mask)
    
    # Find eye positions with better scaling
    # QR code has 3 fixed position detection patterns in corners
    quiet_zone = int(img_size * 0.08)  # Estimate quiet zone (border)
    module_count = 25  # Typical for medium QR codes
    module_size = (img_size - 2 * quiet_zone) / module_count
    
    eye_size = int(module_size * 7)  # Position detection pattern is 7x7 modules
    inner_eye_size = int(module_size * 3)  # Inner eye is 3x3 modules
    
    # Position is based on the fixed positioning patterns of QR codes
    tl_x, tl_y = quiet_zone, quiet_zone  # Top left corner
    tr_x, tr_y = img_size - quiet_zone - eye_size, quiet_zone  # Top right corner
    bl_x, bl_y = quiet_zone, img_size - quiet_zone - eye_size  # Bottom left corner
    
    # Calculate inner eye offset (center point of each eye)
    inner_offset = (eye_size - inner_eye_size) // 2
    
    # Draw inner eye masks with additional border for better detection
    offset_adjust = int(module_size * 0.2)  # Small adjustment for better coverage
    
    # Top left inner eye
    draw.rectangle((
        tl_x + inner_offset - offset_adjust, 
        tl_y + inner_offset - offset_adjust, 
        tl_x + inner_offset + inner_eye_size + offset_adjust, 
        tl_y + inner_offset + inner_eye_size + offset_adjust
    ), fill=255)
    
    # Top right inner eye
    draw.rectangle((
        tr_x + inner_offset - offset_adjust, 
        tr_y + inner_offset - offset_adjust, 
        tr_x + inner_offset + inner_eye_size + offset_adjust, 
        tr_y + inner_offset + inner_eye_size + offset_adjust
    ), fill=255)
    
    # Bottom left inner eye
    draw.rectangle((
        bl_x + inner_offset - offset_adjust, 
        bl_y + inner_offset - offset_adjust, 
        bl_x + inner_offset + inner_eye_size + offset_adjust, 
        bl_y + inner_offset + inner_eye_size + offset_adjust
    ), fill=255)
    
    return mask

def create_outer_eye_mask(img):
    """Create mask for outer eyes with improved accuracy and size scaling"""
    img_size = img.size[0]
    mask = Image.new('L', img.size, 0)
    draw = ImageDraw.Draw(mask)
    
    # Find eye positions with better scaling
    quiet_zone = int(img_size * 0.08)  # Estimate quiet zone (border)
    module_count = 25  # Typical for medium QR codes
    module_size = (img_size - 2 * quiet_zone) / module_count
    
    eye_size = int(module_size * 7)  # Position detection pattern is 7x7 modules
    inner_eye_size = int(module_size * 3)  # Inner eye is 3x3 modules
    
    # Position is based on the fixed positioning patterns of QR codes
    tl_x, tl_y = quiet_zone, quiet_zone  # Top left corner
    tr_x, tr_y = img_size - quiet_zone - eye_size, quiet_zone  # Top right corner
    bl_x, bl_y = quiet_zone, img_size - quiet_zone - eye_size  # Bottom left corner
    
    inner_offset = (eye_size - inner_eye_size) // 2
    
    # Draw outer eye masks with small outward expansion for better detection
    offset_adjust = int(module_size * 0.2)  # Small adjustment for better coverage
    
    # Draw outer eye masks (complete eye areas)
    draw.rectangle((
        tl_x - offset_adjust, 
        tl_y - offset_adjust, 
        tl_x + eye_size + offset_adjust, 
        tl_y + eye_size + offset_adjust
    ), fill=255)  # top left
    
    draw.rectangle((
        tr_x - offset_adjust, 
        tr_y - offset_adjust, 
        tr_x + eye_size + offset_adjust, 
        tr_y + eye_size + offset_adjust
    ), fill=255)  # top right
    
    draw.rectangle((
        bl_x - offset_adjust, 
        bl_y - offset_adjust, 
        bl_x + eye_size + offset_adjust, 
        bl_y + eye_size + offset_adjust
    ), fill=255)  # bottom left
    
    # Cut out inner eyes with exact dimensions - no adjustment here
    draw.rectangle((
        tl_x + inner_offset, 
        tl_y + inner_offset, 
        tl_x + inner_offset + inner_eye_size, 
        tl_y + inner_offset + inner_eye_size
    ), fill=0)  # top left
    
    draw.rectangle((
        tr_x + inner_offset, 
        tr_y + inner_offset, 
        tr_x + inner_offset + inner_eye_size, 
        tr_y + inner_offset + inner_eye_size
    ), fill=0)  # top right
    
    draw.rectangle((
        bl_x + inner_offset, 
        bl_y + inner_offset, 
        bl_x + inner_offset + inner_eye_size, 
        bl_y + inner_offset + inner_eye_size
    ), fill=0)  # bottom left
    
    return mask

# Fix 2: Improved hex color validation and conversion
def hex_to_rgb(hex_color):
    """
    Convert hex color to RGB tuple with robust error handling.
    """
    try:
        # Remove the # symbol if present
        hex_color = hex_color.lstrip('#')
        
        # Check if the hex color is valid
        if not all(c in '0123456789ABCDEFabcdef' for c in hex_color):
            print(f"Invalid hex color: {hex_color}, defaulting to black")
            return (0, 0, 0)
            
        # Handle different hex formats
        if len(hex_color) == 3:
            # Expand 3-digit hex to 6-digit
            hex_color = ''.join(c + c for c in hex_color)
        
        # Convert hex to RGB
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        
        print(f"Converted {hex_color} to RGB: ({r}, {g}, {b})")
        return (r, g, b)
    except Exception as e:
        print(f"Error converting hex color {hex_color}: {str(e)}")
        return (0, 0, 0)  # Return black as fallback

    
def apply_gradient(img, options):
    """Apply gradient to QR code with improved implementation and eye color handling"""
    try:
        # Get gradient parameters
        start_color = hex_to_rgb(options.get('gradient_start', '#FF5500'))
        end_color = hex_to_rgb(options.get('gradient_end', '#FFAA00'))
        gradient_type = options.get('gradient_type', 'linear')
        gradient_direction = options.get('gradient_direction', 'to-right')
        
        # Get whether custom eyes will be applied later
        will_apply_custom_eyes = options.get('custom_eyes', False)
        
        # Print debug info
        print(f"Applying gradient with start color: {start_color}, end color: {end_color}")
        print(f"Gradient type: {gradient_type}, direction: {gradient_direction}")
        print(f"Will apply custom eyes later: {will_apply_custom_eyes}")
        
        width, height = img.size
        
        # Create a new image with the same size
        gradient_img = Image.new('RGBA', img.size, (0, 0, 0, 0))
        
        # Get QR code mask (where the QR modules are)
        mask = Image.new('L', img.size, 0)
        
        # Use the original color to determine what's part of the QR code
        qr_color = hex_to_rgb(options.get('color', '#000000'))
        
        # Calculate eye dimensions for eye preservation
        img_size = img.size[0]
        quiet_zone = int(img_size * 0.08)  # Estimate quiet zone (border)
        module_count = 25  # Typical for medium QR codes
        module_size = (img_size - 2 * quiet_zone) / module_count
        eye_size = int(module_size * 7)  # Position detection pattern is 7x7 modules
        
        # Eye positions
        tl_x, tl_y = quiet_zone, quiet_zone  # Top left
        tr_x, tr_y = img_size - quiet_zone - eye_size, quiet_zone  # Top right
        bl_x, bl_y = quiet_zone, img_size - quiet_zone - eye_size  # Bottom left
        
        # Create eye mask
        eye_mask = Image.new('L', img.size, 0)
        draw = ImageDraw.Draw(eye_mask)
        
        # Draw eye areas - always create the eye mask regardless of custom eyes setting
        draw.rectangle([tl_x, tl_y, tl_x + eye_size, tl_y + eye_size], fill=255)
        draw.rectangle([tr_x, tr_y, tr_x + eye_size, tr_y + eye_size], fill=255)
        draw.rectangle([bl_x, bl_y, bl_x + eye_size, bl_y + eye_size], fill=255)
        
        # Create QR mask, identify all modules
        for y in range(height):
            for x in range(width):
                pixel = img.getpixel((x, y))
                # Check if the pixel matches the QR code color (with some tolerance)
                if (len(pixel) >= 3 and
                    abs(pixel[0] - qr_color[0]) < 30 and 
                    abs(pixel[1] - qr_color[1]) < 30 and 
                    abs(pixel[2] - qr_color[2]) < 30):
                    mask.putpixel((x, y), 255)
        
        # Apply different gradient types
        if gradient_type == 'radial':
            # Radial gradient
            center_x, center_y = width // 2, height // 2
            max_distance = math.sqrt(center_x**2 + center_y**2)
            
            for y in range(height):
                for x in range(width):
                    # Calculate distance from center (normalized 0-1)
                    distance = math.sqrt((x - center_x)**2 + (y - center_y)**2) / max_distance
                    
                    # Interpolate color
                    color = (
                        int(start_color[0] + (end_color[0] - start_color[0]) * distance),
                        int(start_color[1] + (end_color[1] - start_color[1]) * distance),
                        int(start_color[2] + (end_color[2] - start_color[2]) * distance),
                        255
                    )
                    
                    # If pixel is part of QR module but not an eye (when custom eyes will not be applied),
                    # apply gradient color
                    if mask.getpixel((x, y)) > 0:
                        if will_apply_custom_eyes or eye_mask.getpixel((x, y)) == 0:
                            gradient_img.putpixel((x, y), color)
                        
        else:  # Linear gradient (default)
            # Determine gradient direction
            if gradient_direction == 'to-bottom':
                # Vertical gradient (top to bottom)
                for y in range(height):
                    for x in range(width):
                        pos = y / (height - 1) if height > 1 else 0
                        
                        color = (
                            int(start_color[0] + (end_color[0] - start_color[0]) * pos),
                            int(start_color[1] + (end_color[1] - start_color[1]) * pos),
                            int(start_color[2] + (end_color[2] - start_color[2]) * pos),
                            255
                        )
                        
                        if mask.getpixel((x, y)) > 0:
                            if will_apply_custom_eyes or eye_mask.getpixel((x, y)) == 0:
                                gradient_img.putpixel((x, y), color)
                            
            elif gradient_direction == 'to-right-bottom':
                # Diagonal gradient (top-left to bottom-right)
                for y in range(height):
                    for x in range(width):
                        pos = (x / (width - 1) + y / (height - 1)) / 2 if (width > 1 and height > 1) else 0
                        
                        color = (
                            int(start_color[0] + (end_color[0] - start_color[0]) * pos),
                            int(start_color[1] + (end_color[1] - start_color[1]) * pos),
                            int(start_color[2] + (end_color[2] - start_color[2]) * pos),
                            255
                        )
                        
                        if mask.getpixel((x, y)) > 0:
                            if will_apply_custom_eyes or eye_mask.getpixel((x, y)) == 0:
                                gradient_img.putpixel((x, y), color)
            else:
                # Default: horizontal gradient (left to right)
                for y in range(height):
                    for x in range(width):
                        pos = x / (width - 1) if width > 1 else 0
                        
                        color = (
                            int(start_color[0] + (end_color[0] - start_color[0]) * pos),
                            int(start_color[1] + (end_color[1] - start_color[1]) * pos),
                            int(start_color[2] + (end_color[2] - start_color[2]) * pos),
                            255
                        )
                        
                        if mask.getpixel((x, y)) > 0:
                            if will_apply_custom_eyes or eye_mask.getpixel((x, y)) == 0:
                                gradient_img.putpixel((x, y), color)
        
        # Combine gradient with background
        bg_color = hex_to_rgb(options.get('background_color', '#FFFFFF'))
        bg_img = Image.new('RGBA', img.size, bg_color + (255,))
        
        # First composite the gradient onto the background
        result = Image.alpha_composite(bg_img, gradient_img)
        
        # Copy original eye patterns from original image if not using custom eyes
        if not will_apply_custom_eyes:
            # Convert original QR color to RGBA
            original_qr_color = hex_to_rgb(options.get('color', '#000000')) + (255,)
            
            # Create a new image for eyes with solid color
            eye_img = Image.new('RGBA', img.size, (0, 0, 0, 0))
            
            # Copy eye patterns in solid color
            for y in range(height):
                for x in range(width):
                    if eye_mask.getpixel((x, y)) > 0 and mask.getpixel((x, y)) > 0:
                        eye_img.putpixel((x, y), original_qr_color)
            
            # Composite eyes over the result
            result = Image.alpha_composite(result, eye_img)
        
        return result
    except Exception as e:
        print(f"Error applying gradient: {str(e)}")
        import traceback
        traceback.print_exc()
        # Return the original image if there's an error
        return img
    
def apply_custom_eyes(qr, qr_img, options):
    """Applies custom eye styling with improved inner/outer proportions and color management"""
    try:
        # Get dimensions and styling options
        img_width, img_height = qr_img.size
        
        # Determine QR code module count
        module_count = len(qr.modules) if qr else 25  # Default to 25 if no qr object
        quiet_zone = options.get('quiet_zone', 4)
        
        # Calculate module size for precise positioning
        module_size = (min(img_width, img_height)) / (module_count + (2 * quiet_zone))
        
        # Get color options with proper validation
        main_color = options.get('color', '#000000')
        inner_eye_color = options.get('inner_eye_color', main_color)
        outer_eye_color = options.get('outer_eye_color', main_color)
        background_color = options.get('background_color', '#FFFFFF')
        
        # Debug color values
        print(f"Custom eyes - Main color: {main_color}")
        print(f"Custom eyes - Inner eye color: {inner_eye_color}")
        print(f"Custom eyes - Outer eye color: {outer_eye_color}")
        
        # Convert colors to RGB tuples with validation
        inner_eye_rgb = hex_to_rgb(inner_eye_color)
        outer_eye_rgb = hex_to_rgb(outer_eye_color)
        bg_rgb = hex_to_rgb(background_color)
        
        # Get eye style options
        inner_eye_style = options.get('inner_eye_style', 'square')
        outer_eye_style = options.get('outer_eye_style', 'square')
        
        # Debug eye styles
        print(f"Custom eyes - Inner eye style: {inner_eye_style}")
        print(f"Custom eyes - Outer eye style: {outer_eye_style}")
        
        # Calculate eye dimensions - IMPROVED PROPORTIONS
        eye_size = int(7 * module_size)  # Standard size for positioning patterns
        inner_size = int(5 * module_size)  # Increased for better proportions
        
        # Print dimension info for debugging
        print(f"Eye dimensions - Total eye size: {eye_size}px, Inner size: {inner_size}px")
        
        # Create a copy of the QR code to work with
        result = qr_img.copy().convert('RGBA')
        
        # Calculate precise eye positions
        first_module_pos = int(quiet_zone * module_size)
        last_module_pos = int(img_width - quiet_zone * module_size - eye_size)
        
        # Define eye positions
        eye_positions = [
            # Top-left
            (first_module_pos, first_module_pos),
            # Top-right
            (last_module_pos, first_module_pos),
            # Bottom-left
            (first_module_pos, last_module_pos)
        ]
        
        # Process each eye position
        for eye_x, eye_y in eye_positions:
            # Completely clear the original eye area with background color plus 1px buffer
            result_draw = ImageDraw.Draw(result)
            result_draw.rectangle(
                [eye_x-2, eye_y-2, eye_x + eye_size+2, eye_y + eye_size+2],  # Added 2px buffer
                fill=bg_rgb + (255,),
                outline=None
            )
            
            # Draw outer eye pattern
            if outer_eye_style == 'square':
                # Square outer eye
                result_draw.rectangle(
                    [eye_x, eye_y, eye_x + eye_size, eye_y + eye_size],
                    fill=outer_eye_rgb + (255,),
                    outline=None
                )
                
                # Cut out middle for inner eye
                inner_margin = (eye_size - inner_size) // 2
                result_draw.rectangle(
                    [eye_x + inner_margin, eye_y + inner_margin, 
                     eye_x + inner_margin + inner_size, eye_y + inner_margin + inner_size],
                    fill=bg_rgb + (255,),
                    outline=None
                )
                
            elif outer_eye_style == 'circle':
                # Circle outer eye
                result_draw.ellipse(
                    [eye_x, eye_y, eye_x + eye_size, eye_y + eye_size],
                    fill=outer_eye_rgb + (255,),
                    outline=None
                )
                
                # Cut out middle for inner eye
                inner_margin = (eye_size - inner_size) // 2
                result_draw.ellipse(
                    [eye_x + inner_margin, eye_y + inner_margin, 
                     eye_x + inner_margin + inner_size, eye_y + inner_margin + inner_size],
                    fill=bg_rgb + (255,),
                    outline=None
                )
                
            elif outer_eye_style == 'rounded':
                # Rounded square outer eye
                corner_radius = eye_size // 5
                draw_rounded_rectangle(
                    result_draw,
                    [eye_x, eye_y, eye_x + eye_size, eye_y + eye_size],
                    radius=corner_radius,
                    fill=outer_eye_rgb + (255,)
                )
                
                # Cut out middle for inner eye
                inner_margin = (eye_size - inner_size) // 2
                inner_corner_radius = corner_radius // 2
                draw_rounded_rectangle(
                    result_draw,
                    [eye_x + inner_margin, eye_y + inner_margin, 
                     eye_x + inner_margin + inner_size, eye_y + inner_margin + inner_size],
                    radius=inner_corner_radius,
                    fill=bg_rgb + (255,)
                )
            
            # Calculate inner eye position and size
            inner_margin = (eye_size - inner_size) // 2
            inner_x = eye_x + inner_margin
            inner_y = eye_y + inner_margin
            
            # Calculate inner eye size (centered in the cut-out area)
            inner_eye_actual_size = int(inner_size * 0.6)  # Increased proportion
            inner_center_offset = (inner_size - inner_eye_actual_size) // 2
            
            inner_center_x = inner_x + inner_center_offset
            inner_center_y = inner_y + inner_center_offset
            
            # Draw inner eye
            if inner_eye_style == 'square':
                result_draw.rectangle(
                    [inner_center_x, inner_center_y, 
                     inner_center_x + inner_eye_actual_size, inner_center_y + inner_eye_actual_size],
                    fill=inner_eye_rgb + (255,),
                    outline=None
                )
                
            elif inner_eye_style == 'circle':
                result_draw.ellipse(
                    [inner_center_x, inner_center_y, 
                     inner_center_x + inner_eye_actual_size, inner_center_y + inner_eye_actual_size],
                    fill=inner_eye_rgb + (255,),
                    outline=None
                )
                
            elif inner_eye_style == 'rounded':
                inner_corner_radius = inner_eye_actual_size // 5
                draw_rounded_rectangle(
                    result_draw,
                    [inner_center_x, inner_center_y, 
                     inner_center_x + inner_eye_actual_size, inner_center_y + inner_eye_actual_size],
                    radius=inner_corner_radius,
                    fill=inner_eye_rgb + (255,)
                )
        
        return result
    except Exception as e:
        print(f"Error applying custom eyes: {str(e)}")
        import traceback
        traceback.print_exc()
        # Return original image if there's an error
        return qr_img

def draw_rounded_rectangle(draw, coords, radius, fill=None):
    """Helper function to draw a rounded rectangle"""
    x1, y1, x2, y2 = coords
    
    # Draw the main rectangles
    draw.rectangle([x1 + radius, y1, x2 - radius, y2], fill=fill)
    draw.rectangle([x1, y1 + radius, x2, y2 - radius], fill=fill)
    
    # Draw the corner arcs
    draw.pieslice([x1, y1, x1 + radius * 2, y1 + radius * 2], 180, 270, fill=fill)
    draw.pieslice([x2 - radius * 2, y1, x2, y1 + radius * 2], 270, 360, fill=fill)
    draw.pieslice([x1, y2 - radius * 2, x1 + radius * 2, y2], 90, 180, fill=fill)
    draw.pieslice([x2 - radius * 2, y2 - radius * 2, x2, y2], 0, 90, fill=fill)

def generate_svg_qr(qr, options):
    """Generate SVG version of QR code"""
    qr_matrix = qr.get_matrix()
    shape = options.get('shape', 'square')
    
    # Convert colors
    fill_color = options.get('color', '#000000')
    bg_color = options.get('background_color', '#FFFFFF')
    
    # Calculate dimensions
    module_count = len(qr_matrix)
    box_size = options.get('module_size', 10)
    border = options.get('quiet_zone', 4)
    size = module_count * box_size + border * 2 * box_size
    
    # Prepare for gradient if requested
    has_gradient = options.get('gradient_start') and options.get('gradient_end')
    gradient_id = f"gradient-{uuid.uuid4()}"
    
    # Start SVG
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}">',
    ]
    
    # Add gradient definition if needed
    if has_gradient:
        start_color = options.get('gradient_start')
        end_color = options.get('gradient_end')
        svg.append(f'<defs><linearGradient id="{gradient_id}" x1="0%" y1="0%" x2="100%" y2="100%">')
        svg.append(f'<stop offset="0%" stop-color="{start_color}"/>')
        svg.append(f'<stop offset="100%" stop-color="{end_color}"/>')
        svg.append(f'</linearGradient></defs>')
        fill_color = f"url(#{gradient_id})"
    
    # Add background
    svg.append(f'<rect width="{size}" height="{size}" fill="{bg_color}"/>')
    
    # Draw modules
    for r, row in enumerate(qr_matrix):
        for c, val in enumerate(row):
            if val:
                x, y = c * box_size + border * box_size, r * box_size + border * box_size
                
                if shape == 'square':
                    svg.append(f'<rect x="{x}" y="{y}" width="{box_size}" height="{box_size}" fill="{fill_color}"/>')
                elif shape == 'rounded':
                    radius = box_size / 4
                    svg.append(f'<rect x="{x}" y="{y}" width="{box_size}" height="{box_size}" rx="{radius}" ry="{radius}" fill="{fill_color}"/>')
                elif shape == 'circle':
                    cx, cy = x + box_size / 2, y + box_size / 2
                    radius = box_size / 2
                    svg.append(f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="{fill_color}"/>')
                elif shape == 'diamond':
                    cx, cy = x + box_size / 2, y + box_size / 2
                    points = f"{cx},{y} {x+box_size},{cy} {cx},{y+box_size} {x},{cy}"
                    svg.append(f'<polygon points="{points}" fill="{fill_color}"/>')
    
    # Apply custom eyes if requested
    if options.get('custom_eyes'):
        # This would require a more complex SVG generation approach
        # For simplicity, we'll skip custom eyes in SVG format
        pass
    
    # Add logo if provided
    logo_path = options.get('logo_path')
    if logo_path and os.path.exists(logo_path):
        try:
            # For SVG, we'll embed the logo as base64
            with open(logo_path, "rb") as image_file:
                logo_data = base64.b64encode(image_file.read()).decode()
            
            logo_mime = "image/png"  # Assume PNG, could detect from file extension
            logo_size = size // 4
            logo_x = (size - logo_size) // 2
            logo_y = (size - logo_size) // 2
            
            svg.append(f'<image x="{logo_x}" y="{logo_y}" width="{logo_size}" height="{logo_size}" '+
                      f'href="data:{logo_mime};base64,{logo_data}" />')
        except Exception as e:
            print(f"Error adding logo to SVG: {str(e)}")
    
    # Add watermark if specified
    watermark_text = options.get('watermark_text')
    if watermark_text:
        font_size = size // 30
        svg.append(f'<text x="{size - 10}" y="{size - 10}" font-size="{font_size}" '
                  f'fill="rgba(0,0,0,0.3)" text-anchor="end">{watermark_text}</text>')
    
    # Close SVG
    svg.append('</svg>')
    
    svg_str = ''.join(svg)
    return f"data:image/svg+xml;base64,{base64.b64encode(svg_str.encode()).decode()}"


def apply_logo(qr_img, logo_path, round_corners=False, size_percentage=25):
    """
    Apply logo to center of QR code - FIXED VERSION with better path resolution
    """
    try:
        print(f"=== APPLYING LOGO ===")
        print(f"Input logo path: {logo_path}")
        print(f"QR image size: {qr_img.size}")
        
        # Validate inputs
        if not logo_path or not logo_path.strip():
            print("Logo path is empty or None")
            return qr_img
        
        # Find the actual logo file with comprehensive path checking
        full_logo_path = None
        
        # Define all possible paths where the logo might be
        upload_folder = app.config.get('UPLOAD_FOLDER', 'static/uploads')
        possible_paths = [
            # Direct path (if absolute)
            logo_path,
            # Relative to upload folder
            os.path.join(upload_folder, logo_path),
            # Handle if logo_path already includes upload folder
            os.path.join(upload_folder, logo_path.replace('static/uploads/', '')),
            # Static folder variations
            os.path.join('static', 'uploads', logo_path),
            os.path.join('static', logo_path),
            # Just filename in logos directory
            os.path.join(upload_folder, 'logos', os.path.basename(logo_path)),
            # Current working directory + relative path
            os.path.abspath(logo_path),
            os.path.abspath(os.path.join(upload_folder, logo_path))
        ]
        
        print(f"Searching for logo in {len(possible_paths)} possible locations...")
        
        for i, path in enumerate(possible_paths):
            print(f"  {i+1}. Checking: {path}")
            if path and os.path.exists(path) and os.path.isfile(path):
                file_size = os.path.getsize(path)
                if file_size > 0:
                    full_logo_path = path
                    print(f"  ✓ Found valid logo: {path} (size: {file_size} bytes)")
                    break
                else:
                    print(f"  ✗ File exists but is empty: {path}")
            else:
                print(f"  ✗ Not found or not a file")
        
        if not full_logo_path:
            print(f"ERROR: Logo file not found in any expected location")
            print(f"Original path: {logo_path}")
            print(f"Upload folder: {upload_folder}")
            return qr_img
        
        # Open and validate the logo image
        try:
            logo = Image.open(full_logo_path).convert('RGBA')
            print(f"Logo opened successfully: {logo.size} pixels, mode: {logo.mode}")
            
            # Validate image dimensions
            if logo.size[0] == 0 or logo.size[1] == 0:
                print("Logo has invalid dimensions")
                return qr_img
                
        except Exception as img_error:
            print(f"Error opening logo image: {str(img_error)}")
            return qr_img
        
        # Calculate logo size (ensure reasonable limits)
        qr_width, qr_height = qr_img.size
        size_percentage = max(10, min(40, size_percentage))  # Limit to 10-40%
        
        # Calculate maximum logo size
        max_logo_dimension = min(qr_width, qr_height) * size_percentage // 100
        print(f"QR size: {qr_width}x{qr_height}, max logo size: {max_logo_dimension}px ({size_percentage}%)")
        
        # Resize logo maintaining aspect ratio
        logo_width, logo_height = logo.size
        scale_factor = min(max_logo_dimension / logo_width, max_logo_dimension / logo_height)
        
        new_logo_width = max(1, int(logo_width * scale_factor))
        new_logo_height = max(1, int(logo_height * scale_factor))
        
        print(f"Resizing logo from {logo_width}x{logo_height} to {new_logo_width}x{new_logo_height}")
        
        logo = logo.resize((new_logo_width, new_logo_height), Image.Resampling.LANCZOS)
        
        # Apply rounded corners if requested
        if round_corners:
            print("Applying rounded corners to logo")
            corner_radius = min(new_logo_width, new_logo_height) // 6
            
            # Create rounded mask
            mask = Image.new('L', (new_logo_width, new_logo_height), 0)
            draw = ImageDraw.Draw(mask)
            draw.rounded_rectangle(
                [(0, 0), (new_logo_width-1, new_logo_height-1)], 
                radius=corner_radius, 
                fill=255
            )
            
            # Apply mask to logo
            rounded_logo = Image.new('RGBA', (new_logo_width, new_logo_height), (0, 0, 0, 0))
            rounded_logo.paste(logo, (0, 0))
            rounded_logo.putalpha(mask)
            logo = rounded_logo
        
        # Create a copy of the QR code
        result = qr_img.copy()
        if result.mode != 'RGBA':
            result = result.convert('RGBA')
        
        # Calculate center position for logo
        x = (qr_width - new_logo_width) // 2
        y = (qr_height - new_logo_height) // 2
        
        print(f"Placing logo at position: ({x}, {y})")
        
        # Paste logo onto QR code
        result.paste(logo, (x, y), logo)
        
        print("Logo applied successfully")
        return result
        
    except Exception as e:
        print(f"Error in apply_logo: {str(e)}")
        import traceback
        traceback.print_exc()
        return qr_img


def add_corners(im, rad):
    """
    Adds rounded corners to a given PIL Image while preserving transparency.
    
    Args:
        im (PIL.Image.Image): The input image to which rounded corners will be applied.
        rad (int): The radius of the rounded corners.
    
    Returns:
        PIL.Image.Image: A new image with rounded corners and preserved transparency.
    """
    circle = Image.new('L', (rad * 2, rad * 2), 0)
    draw = ImageDraw.Draw(circle)
    draw.ellipse((0, 0, rad * 2 - 1, rad * 2 - 1), fill=255)
    
    alpha = Image.new('L', im.size, 255)
    w, h = im.size
    alpha.paste(circle.crop((0, 0, rad, rad)), (0, 0))
    alpha.paste(circle.crop((0, rad, rad, rad * 2)), (0, h - rad))
    alpha.paste(circle.crop((rad, 0, rad * 2, rad)), (w - rad, 0))
    alpha.paste(circle.crop((rad, rad, rad * 2, rad * 2)), (w - rad, h - rad))
    
    # Preserve original alpha channel if it exists
    if im.mode == 'RGBA':
        original_alpha = im.getchannel('A')
        alpha = ImageChops.multiply(original_alpha, alpha)
    
    # Create a new image with the rounded alpha channel
    result = im.copy()
    result.putalpha(alpha)
    
    return result


def fix_logo_path_handling(request, qr_code):
    """Handle logo upload with robust path handling and validation - FIXED VERSION"""
    logo_path = qr_code.logo_path  # Keep existing path by default
    
    # Handle logo upload
    if 'logo' in request.files and request.files['logo'].filename:
        logo_file = request.files['logo']
        
        # Validate file size (limit to 5MB)
        if hasattr(logo_file, 'content_length') and logo_file.content_length > 5 * 1024 * 1024:
            print("Logo file too large (>5MB)")
            return logo_path
        
        # Remove old logo if it exists
        if qr_code.logo_path:
            try:
                old_logo_paths = [
                    qr_code.logo_path,
                    os.path.join(app.config['UPLOAD_FOLDER'], qr_code.logo_path),
                    os.path.join('static', 'uploads', qr_code.logo_path)
                ]
                
                for old_path in old_logo_paths:
                    if os.path.exists(old_path):
                        os.remove(old_path)
                        print(f"Removed old logo: {old_path}")
                        break
            except Exception as e:
                print(f"Error removing old logo: {e}")
        
        # Create upload directory structure
        upload_base = app.config.get('UPLOAD_FOLDER', 'static/uploads')
        upload_dir = os.path.join(upload_base, 'logos')
        
        # Ensure directory exists
        try:
            os.makedirs(upload_dir, exist_ok=True)
            print(f"Created/verified directory: {upload_dir}")
        except Exception as e:
            print(f"Error creating upload directory: {e}")
            return logo_path
        
        # Validate file extension
        file_ext = os.path.splitext(logo_file.filename)[1].lower()
        valid_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']
        
        if not file_ext or file_ext not in valid_extensions:
            file_ext = '.png'  # Default to PNG
        
        # Create unique filename
        filename = f"logo_{uuid.uuid4().hex[:8]}{file_ext}"
        full_path = os.path.join(upload_dir, filename)
        
        # Save the file
        try:
            # Read file content to verify it's not empty
            file_content = logo_file.read()
            if len(file_content) == 0:
                print("Logo file is empty")
                return logo_path
            
            # Reset file pointer and save
            logo_file.seek(0)
            logo_file.save(full_path)
            
            # Verify file was saved correctly
            if not os.path.exists(full_path):
                print(f"File was not saved: {full_path}")
                return logo_path
            
            if os.path.getsize(full_path) == 0:
                print(f"Saved file is empty: {full_path}")
                os.remove(full_path)
                return logo_path
            
            # Test that we can open the image
            try:
                from PIL import Image
                test_img = Image.open(full_path)
                test_img.verify()  # Verify image integrity
                print(f"Logo image verified successfully: {test_img.size}")
            except Exception as img_error:
                print(f"Invalid image file: {img_error}")
                os.remove(full_path)
                return logo_path
            
            # Store relative path for database
            relative_path = os.path.join('logos', filename)
            print(f"Logo saved successfully: {full_path} -> stored as: {relative_path}")
            return relative_path
            
        except Exception as e:
            print(f"Error saving logo file: {e}")
            import traceback
            traceback.print_exc()
            
            # Clean up if file was partially created
            if os.path.exists(full_path):
                try:
                    os.remove(full_path)
                except:
                    pass
    
    return logo_path


def generate_qr_code(qr_code):
    """Generate QR code image with improved color handling and eye customization - FIXED VERSION"""
    try:
        print("=== STARTING QR CODE GENERATION ===")
        
        # Get QR data
        qr_data = generate_qr_data(qr_code)
        print(f"QR data: {qr_data[:100]}...")  # Show first 100 chars
        
        # Get QR options
        options = get_qr_options(qr_code)
        
        # Apply fixes to ensure template doesn't override user choices
        options = fix_template_override_issues(qr_code, options)
        
        # Set up QR code generator with proper quiet zone
        module_size = options.get('module_size', 10)
        quiet_zone = options.get('quiet_zone', 4)
        
        qr = qrcode.QRCode(
            version=None,
            error_correction={
                'L': qrcode.constants.ERROR_CORRECT_L,
                'M': qrcode.constants.ERROR_CORRECT_M,
                'Q': qrcode.constants.ERROR_CORRECT_Q,
                'H': qrcode.constants.ERROR_CORRECT_H
            }.get(options.get('error_correction', 'H'), qrcode.constants.ERROR_CORRECT_H),
            box_size=module_size,
            border=quiet_zone
        )
        
        qr.add_data(qr_data)
        qr.make(fit=True)
        
        # Get shape and validated colors
        shape = options.get('shape', 'square')
        color = options.get('color', '#000000')
        background_color = options.get('background_color', '#FFFFFF')
        
        # Convert colors to RGB tuples
        color_rgb = hex_to_rgb(color)
        bg_color_rgb = hex_to_rgb(background_color)
        
        # Determine if we need gradient or custom eyes
        using_gradient = options.get('using_gradient', False)
        using_custom_eyes = options.get('custom_eyes', False)
        
        print(f"QR settings - Gradient: {using_gradient}, Custom eyes: {using_custom_eyes}")
        print(f"Colors - QR: {color}, Background: {background_color}")
        
        # Generate the basic QR image
        try:
            module_drawer = get_module_drawer(shape)
            qr_img = qr.make_image(
                image_factory=StyledPilImage,
                module_drawer=module_drawer,
                color_mask=SolidFillColorMask(
                    front_color=color_rgb,
                    back_color=bg_color_rgb
                )
            ).convert('RGBA')
            print(f"Generated basic QR with shape: {shape}")
        except Exception as style_error:
            print(f"Error with styled QR generation: {str(style_error)}")
            qr_img = qr.make_image(
                fill_color=color,
                back_color=background_color
            ).convert('RGBA')
            print("Fallback to basic QR generation")
        
        # Apply special effects in sequence
        
        # 1. Apply gradient if needed
        if using_gradient:
            options['will_apply_custom_eyes'] = using_custom_eyes
            try:
                print("Applying gradient to QR code")
                qr_img = apply_gradient(qr_img, options)
                print("Gradient applied successfully")
            except Exception as gradient_error:
                print(f"Error applying gradient: {str(gradient_error)}")
                import traceback
                traceback.print_exc()
        
        # 2. Apply custom eyes if needed
        if using_custom_eyes:
            try:
                print("Applying custom eyes")
                qr_img = apply_custom_eyes(qr, qr_img, options)
                print("Custom eyes applied successfully")
            except Exception as eye_error:
                print(f"Error applying custom eyes: {str(eye_error)}")
                import traceback
                traceback.print_exc()
        
        # 3. FIXED LOGO APPLICATION
        logo_path = options.get('logo_path')
        if logo_path:
            print(f"Attempting to apply logo: {logo_path}")
            
            try:
                qr_img = apply_logo(
                    qr_img, 
                    logo_path,
                    round_corners=options.get('round_logo', False),
                    size_percentage=options.get('logo_size_percentage', 25)
                )
                print("Logo applied successfully")
            except Exception as logo_error:
                print(f"Error applying logo: {str(logo_error)}")
                import traceback
                traceback.print_exc()
        else:
            print("No logo path provided")
        
        # 4. Apply frame if specified
        if options.get('frame_type'):
            try:
                qr_img = apply_frame(qr_img, options.get('frame_type'), options)
                print(f"Frame applied: {options.get('frame_type')}")
            except Exception as frame_error:
                print(f"Error applying frame: {str(frame_error)}")
        
        # 5. Apply watermark if specified
        if options.get('watermark_text'):
            try:
                qr_img = apply_watermark(qr_img, options.get('watermark_text'))
                print("Watermark applied")
            except Exception as watermark_error:
                print(f"Error applying watermark: {str(watermark_error)}")
        
        # FIXED: Ensure image is valid before conversion
        if not qr_img or qr_img.size[0] == 0 or qr_img.size[1] == 0:
            raise Exception("Generated QR image is invalid or empty")
        
        print(f"Final QR image size: {qr_img.size}")
        
        # Convert to base64 string with validation
        buffered = BytesIO()
        qr_img.save(buffered, format="PNG", optimize=True)
        buffered.seek(0)
        
        img_data = buffered.getvalue()
        if len(img_data) == 0:
            raise Exception("Generated PNG data is empty")
        
        img_str = base64.b64encode(img_data).decode()
        print(f"Generated base64 string length: {len(img_str)}")
        
        # Return data URL and QR info
        qr_info = {
            'version': qr.version,
            'error_correction': options.get('error_correction', 'H'),
            'module_count': len(qr.modules),
            'format': 'png',
            'color': color,
            'size': qr_img.size,
            'data_length': len(img_data),
            'has_logo': bool(logo_path),
            'eye_colors': {
                'inner': options.get('inner_eye_color', color),
                'outer': options.get('outer_eye_color', color)
            }
        }
        
        print("=== QR CODE GENERATION COMPLETE ===")
        return f"data:image/png;base64,{img_str}", qr_info
        
    except Exception as e:
        print(f"Error generating QR code: {str(e)}")
        import traceback
        traceback.print_exc()
        
        # Create fallback QR code
        try:
            error_qr = qrcode.make(f"Error: {str(e)[:50]}").get_image()
            buffered = BytesIO()
            error_qr.save(buffered, format="PNG")
            img_str = base64.b64encode(buffered.getvalue()).decode()
            
            qr_info = {
                'version': 1,
                'error_correction': 'H',
                'module_count': 25,
                'format': 'png',
                'error': str(e),
                'size': error_qr.size
            }
            
            return f"data:image/png;base64,{img_str}", qr_info
        except:
            # Ultimate fallback
            return "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", {'error': 'Complete failure to generate QR code'}

def batch_generate_qr(data_list, output_format="zip"):
    """
    Generate multiple QR codes at once
    
    Parameters:
    data_list (list): List of dictionaries containing:
        - data: QR code data
        - options: Individual QR code options (optional)
        - label: Name for the QR code file (optional)
    output_format (str): Output format: "zip" or "pdf"
    
    Returns:
    str: Base64 encoded zip or PDF file
    """
    try:
        # Generate multiple QR codes
        qr_images = []
        for item in data_list:
            data = item['data']
            item_options = item.get('options', {})
            
            # Create QR code
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_H,
                box_size=item_options.get('module_size', 10),
                border=item_options.get('quiet_zone', 4),
            )
            qr.add_data(data)
            qr.make(fit=True)
            
            # Generate image
            fill_color = item_options.get('color', '#000000')
            bg_color = item_options.get('background_color', '#FFFFFF')
            img = qr.make_image(fill_color=fill_color, back_color=bg_color).convert('RGBA')
            
            # Apply shape if specified
            shape = item_options.get('shape')
            if shape and shape != 'square':
                # Simplified shape application
                width, height = img.size
                mask = Image.new('L', (width, height), 0)
                draw = ImageDraw.Draw(mask)
                
                if shape == 'rounded':
                    radius = width // 10
                    draw.rectangle([radius, radius, width - radius, height - radius], fill=255)
                    draw.rectangle([0, radius, width, height - radius], fill=255)
                    draw.rectangle([radius, 0, width - radius, height], fill=255)
                    draw.pieslice([0, 0, radius * 2, radius * 2], 180, 270, fill=255)
                    draw.pieslice([width - radius * 2, 0, width, radius * 2], 270, 0, fill=255)
                    draw.pieslice([0, height - radius * 2, radius * 2, height], 90, 180, fill=255)
                    draw.pieslice([width - radius * 2, height - radius * 2, width, height], 0, 90, fill=255)
                elif shape == 'circle':
                    draw.ellipse([0, 0, width, height], fill=255)
                
                shaped_img = Image.new('RGBA', (width, height))
                shaped_img.paste(img, (0, 0), mask)
                img = shaped_img
            
            # Apply logo if specified
            logo_path = item_options.get('logo_path')
            if logo_path and os.path.exists(logo_path):
                img = apply_logo(
                    img, 
                    logo_path,
                    round_corners=item_options.get('round_logo', False),
                    size_percentage=item_options.get('logo_size_percentage', 25)
                )
            
            qr_images.append(img)
        
        # Output as requested format
        buffered = BytesIO()
        
        if output_format == "pdf":
            # Create a PDF with multiple QR codes
            try:
                from reportlab.lib.pagesizes import letter
                from reportlab.pdfgen import canvas
                
                # Create PDF
                c = canvas.Canvas(buffered, pagesize=letter)
                width, height = letter
                
                # Calculate grid layout
                margin = 50
                max_per_row = 2
                qr_size = (width - margin*2) // max_per_row
                
                # Add QR codes to PDF
                for i, img in enumerate(qr_images):
                    row = i // max_per_row
                    col = i % max_per_row
                    
                    x = margin + col * qr_size
                    y = height - margin - (row + 1) * qr_size
                    
                    # Convert PIL image to temp file for PDF
                    temp_img = BytesIO()
                    img = img.resize((int(qr_size * 0.9), int(qr_size * 0.9)), Image.LANCZOS)
                    img.save(temp_img, format='PNG')
                    temp_img.seek(0)
                    
                    # Add to PDF
                    c.drawImage(temp_img, x, y, width=qr_size * 0.9, height=qr_size * 0.9)
                    
                    # Add label if available
                    if 'label' in data_list[i]:
                        c.setFont("Helvetica", 10)
                        c.drawString(x, y - 15, data_list[i]['label'])
                    
                    # Create new page if needed
                    if (i + 1) % (max_per_row * 4) == 0 and i < len(qr_images) - 1:
                        c.showPage()
                
                c.save()
                mime_type = "application/pdf"
            except ImportError:
                # Fallback to ZIP if ReportLab is not available
                output_format = "zip"
        
        if output_format == "zip":
            # Create a ZIP file with multiple QR codes
            import zipfile
         
            # Create ZIP file
            with zipfile.ZipFile(buffered, 'w') as zf:
                for i, img in enumerate(qr_images):
                    # Generate filename
                    filename = f"qrcode_{i+1}.png"
                    if 'label' in data_list[i]:
                        safe_label = data_list[i]['label'].replace(' ', '_').replace('/', '_')
                        filename = f"{safe_label}.png"
                    
                    # Save image to temporary buffer
                    temp_img = BytesIO()
                    img.save(temp_img, format='PNG')
                    temp_img.seek(0)
                    
                    # Add to ZIP
                    zf.writestr(filename, temp_img.getvalue())
            
            mime_type = "application/zip"
        
        # Convert to base64
        img_str = base64.b64encode(buffered.getvalue()).decode()
        
        return f"data:{mime_type};base64,{img_str}"
    except Exception as e:
        print(f"Error in batch_generate_qr: {str(e)}")
        # Return a simple error message as base64
        error_msg = f"Error generating batch QR codes: {str(e)}"
        return f"data:text/plain;base64,{base64.b64encode(error_msg.encode()).decode()}"
    


def fix_template_override_issues(qr_code, options):
    """
    Enhanced function to fix issues with template colors overriding user selections.
    FIXED: Use QR code color for gradient fields when in solid color mode.
    """
    # Start with a clean copy of options to avoid modifying the original
    fixed_options = options.copy()
    
    print(f"=== FIXING TEMPLATE OVERRIDE ISSUES ===")
    print(f"Input QR gradient flag: {getattr(qr_code, 'gradient', 'not set')}")
    print(f"Input QR export_type: {getattr(qr_code, 'export_type', 'not set')}")
    print(f"Input QR color: {getattr(qr_code, 'color', 'not set')}")
    print(f"Input QR background_color: {getattr(qr_code, 'background_color', 'not set')}")
    print(f"Input template: {getattr(qr_code, 'template', 'not set')}")
    
    # Always prioritize user's explicitly chosen colors
    if qr_code.color and qr_code.color.strip() and qr_code.color != 'undefined' and qr_code.color != 'null':
        fixed_options['color'] = qr_code.color
        print(f"Using user's explicit color: {qr_code.color}")
    
    if qr_code.background_color and qr_code.background_color.strip() and qr_code.background_color != 'undefined' and qr_code.background_color != 'null':
        fixed_options['background_color'] = qr_code.background_color
        print(f"Using user's explicit background color: {qr_code.background_color}")
    
    # Get the main QR color for fallback use
    main_qr_color = fixed_options.get('color', '#000000')
    
    # CRITICAL FIX: Determine gradient usage with proper logic and precedence
    using_gradient = False
    gradient_start = None
    gradient_end = None
    
    # Priority order for gradient detection:
    # 1. Explicit gradient column (most reliable)
    if hasattr(qr_code, 'gradient') and qr_code.gradient:
        using_gradient = True
        gradient_start = getattr(qr_code, 'gradient_start', None)
        gradient_end = getattr(qr_code, 'gradient_end', None)
        print(f"Gradient detected via gradient column: True")
        
    # 2. Export type check
    elif (hasattr(qr_code, 'export_type') and qr_code.export_type == 'gradient') or fixed_options.get('export_type') == 'gradient':
        using_gradient = True
        gradient_start = getattr(qr_code, 'gradient_start', None) or fixed_options.get('gradient_start')
        gradient_end = getattr(qr_code, 'gradient_end', None) or fixed_options.get('gradient_end')
        print(f"Gradient detected via export_type")
        
    # 3. Using gradient flag
    elif fixed_options.get('using_gradient') == 'true' or fixed_options.get('using_gradient') is True:
        using_gradient = True
        gradient_start = fixed_options.get('gradient_start')
        gradient_end = fixed_options.get('gradient_end')
        print(f"Gradient detected via using_gradient option")
        
    # 4. Template-based gradient detection
    elif hasattr(qr_code, 'template') and qr_code.template and qr_code.template in QR_TEMPLATES:
        template_config = QR_TEMPLATES[qr_code.template]
        if template_config.get('gradient', False) and template_config.get('export_type') == 'gradient':
            using_gradient = True
            gradient_start = template_config.get('gradient_start')
            gradient_end = template_config.get('gradient_end')
            print(f"Gradient detected via template: {qr_code.template}")
    
    # Set the final gradient state
    fixed_options['using_gradient'] = using_gradient
    
    # CRITICAL FIX: Handle gradient vs solid color mode properly
    if using_gradient:
        print("=== CONFIGURING GRADIENT MODE ===")
        fixed_options['export_type'] = 'gradient'
        
        # Ensure we have gradient colors
        if not gradient_start:
            gradient_start = '#f97316'
        if not gradient_end:
            gradient_end = '#fbbf24'
            
        fixed_options['gradient_start'] = gradient_start
        fixed_options['gradient_end'] = gradient_end
        fixed_options['gradient_type'] = getattr(qr_code, 'gradient_type', None) or 'linear'
        fixed_options['gradient_direction'] = getattr(qr_code, 'gradient_direction', None) or 'to-right'
        
        # Auto-enable custom eyes for gradient mode
        fixed_options['custom_eyes'] = True
        fixed_options['using_custom_eyes'] = True
        
        print(f"Gradient colors: {gradient_start} → {gradient_end}")
        print("Auto-enabled custom eyes for gradient mode")
        
    else:
        print("=== CONFIGURING SOLID COLOR MODE ===")
        fixed_options['export_type'] = 'png'
        fixed_options['using_gradient'] = False
        
        # CRITICAL FIX: Set gradient colors to main QR color instead of removing them
        print(f"Setting gradient colors to main QR color: {main_qr_color}")
        fixed_options['gradient_start'] = main_qr_color
        fixed_options['gradient_end'] = main_qr_color
        fixed_options['gradient_type'] = 'linear'  # Keep type for consistency
        fixed_options['gradient_direction'] = 'to-right'  # Keep direction for consistency
        
        # For non-gradient QR codes, respect user's custom eyes preference
        fixed_options['custom_eyes'] = qr_code.custom_eyes if qr_code.custom_eyes is not None else True
        
        print(f"Set gradient fields to QR color: {main_qr_color}")
    
    # Handle custom eyes configuration
    if fixed_options.get('custom_eyes'):
        print("=== CONFIGURING CUSTOM EYES ===")
        
        # Handle eye styles with user preferences first, then sensible defaults
        if hasattr(qr_code, 'inner_eye_style') and qr_code.inner_eye_style:
            fixed_options['inner_eye_style'] = qr_code.inner_eye_style
        elif 'inner_eye_style' not in fixed_options or not fixed_options['inner_eye_style']:
            # Default eye style based on QR shape for harmony
            shape_eye_map = {
                'circle': 'circle',
                'rounded': 'rounded',
                'square': 'square',
                'vertical_bars': 'square',
                'horizontal_bars': 'square',
                'gapped_square': 'square'
            }
            qr_shape = fixed_options.get('shape', 'square')
            fixed_options['inner_eye_style'] = shape_eye_map.get(qr_shape, 'square')
        
        if hasattr(qr_code, 'outer_eye_style') and qr_code.outer_eye_style:
            fixed_options['outer_eye_style'] = qr_code.outer_eye_style
        elif 'outer_eye_style' not in fixed_options or not fixed_options['outer_eye_style']:
            # Match outer style to inner by default, or based on QR shape
            if 'inner_eye_style' in fixed_options:
                fixed_options['outer_eye_style'] = fixed_options['inner_eye_style']
            else:
                shape_eye_map = {
                    'circle': 'circle',
                    'rounded': 'rounded',
                    'square': 'square'
                }
                qr_shape = fixed_options.get('shape', 'square')
                fixed_options['outer_eye_style'] = shape_eye_map.get(qr_shape, 'square')
        
        # Handle eye colors based on user choices, gradient, or main color
        # Inner eye color priority: user choice > gradient start > main color
        if (hasattr(qr_code, 'inner_eye_color') and qr_code.inner_eye_color and 
            qr_code.inner_eye_color.strip() != '' and 
            qr_code.inner_eye_color not in ['undefined', 'null']):
            fixed_options['inner_eye_color'] = qr_code.inner_eye_color
            print(f"Using user's inner eye color: {qr_code.inner_eye_color}")
        elif using_gradient and gradient_start:
            # For gradient QRs, use first gradient color for inner eye
            fixed_options['inner_eye_color'] = gradient_start
            print(f"Using gradient start color for inner eye: {gradient_start}")
        else:
            # Default to main QR color
            fixed_options['inner_eye_color'] = main_qr_color
            print(f"Using main color for inner eye: {main_qr_color}")
        
        # Outer eye color priority: user choice > gradient end > main color
        if (hasattr(qr_code, 'outer_eye_color') and qr_code.outer_eye_color and 
            qr_code.outer_eye_color.strip() != '' and 
            qr_code.outer_eye_color not in ['undefined', 'null']):
            fixed_options['outer_eye_color'] = qr_code.outer_eye_color
            print(f"Using user's outer eye color: {qr_code.outer_eye_color}")
        elif using_gradient and gradient_end:
            # For gradient QRs, use second gradient color for outer eye
            fixed_options['outer_eye_color'] = gradient_end
            print(f"Using gradient end color for outer eye: {gradient_end}")
        else:
            # Default to main QR color
            fixed_options['outer_eye_color'] = main_qr_color
            print(f"Using main color for outer eye: {main_qr_color}")
    else:
        # If custom eyes disabled, REMOVE any eye styles to prevent conflicts
        print("Custom eyes are disabled, removing any eye style settings")
        
        for key in ['inner_eye_style', 'outer_eye_style', 'inner_eye_color', 'outer_eye_color']:
            if key in fixed_options:
                del fixed_options[key]
    
    # Handle frame color if present
    if hasattr(qr_code, 'frame_type') and qr_code.frame_type:
        if (hasattr(qr_code, 'frame_color') and qr_code.frame_color and 
            qr_code.frame_color.strip() != '' and 
            qr_code.frame_color not in ['undefined', 'null']):
            fixed_options['frame_color'] = qr_code.frame_color
            print(f"Using user's frame color: {qr_code.frame_color}")
        else:
            fixed_options['frame_color'] = '#000000'  # Default to black
            print("Setting default black color for frame")
    
    print(f"=== FINAL FIXED OPTIONS ===")
    print(f"using_gradient: {fixed_options.get('using_gradient')}")
    print(f"export_type: {fixed_options.get('export_type')}")
    print(f"color: {fixed_options.get('color')}")
    print(f"background_color: {fixed_options.get('background_color')}")
    print(f"gradient_start: {fixed_options.get('gradient_start', 'not set')}")
    print(f"gradient_end: {fixed_options.get('gradient_end', 'not set')}")
    print(f"custom_eyes: {fixed_options.get('custom_eyes')}")
    print("=== END TEMPLATE OVERRIDE FIX ===")
    
    return fixed_options

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# Add custom Jinja2 filter for JSON parsing
@app.template_filter('fromjson')
def fromjson_filter(value):
    """Convert a JSON string to a Python object"""
    if value:
        try:
            if isinstance(value, str):
                return json.loads(value)
            return value
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}
# ========================
# PUBLIC BLOG ROUTES
# ========================

@app.route('/blogs')
def public_blogs():
    """Public blog listing page"""
    try:
        # Get filter parameters
        category_id = request.args.get('category', type=int)
        page = request.args.get('page', 1, type=int)
        per_page = 9  # Show 9 blogs per page

        # Base query - only active blogs
        query = Blog.query.filter_by(status=True)

        # Filter by category if specified
        if category_id:
            query = query.filter_by(category_id=category_id)

        # Order by latest first
        query = query.order_by(Blog.created_at.desc())

        # Paginate results
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        blogs = pagination.items

        # Get all active categories for filter
        categories = BlogCategory.query.filter_by(status=True).order_by(BlogCategory.sort_order, BlogCategory.name).all()

        # Get the selected category object for schema markup
        selected_category_obj = None
        if category_id:
            selected_category_obj = BlogCategory.query.get(category_id)

        return render_template('blogs.html',
                             blogs=blogs,
                             categories=categories,
                             pagination=pagination,
                             selected_category=category_id,
                             selected_category_obj=selected_category_obj)
    except Exception as e:
        app.logger.error(f"Error loading blogs: {str(e)}")
        flash('Error loading blogs', 'danger')
        return redirect(url_for('index'))

@app.route('/blog/<slug>')
def blog_detail(slug):
    """Individual blog detail page"""
    try:
        # Get blog by slug
        blog = Blog.query.filter_by(slug=slug, status=True).first_or_404()
        # Debug: Log schema_data
        print(f"[DEBUG] Blog '{blog.title}' - ORM schema_data: {blog.schema_data}")

        # If schema_data is None, try to fetch it directly from database
        # This bypasses SQLAlchemy ORM reflection issues
        if blog.schema_data is None:
            from sqlalchemy import text
            result = db.session.execute(
                text("SELECT schema_data FROM blogs WHERE id = :id"),
                {"id": blog.id}
            ).fetchone()
            print(f"[DEBUG] Raw SQL result for blog {blog.id}: {result}")
            if result and result[0]:
                blog.schema_data = result[0]
                print(f"[DEBUG] schema_data loaded successfully: {blog.schema_data[:100]}...")
            else:
                print(f"[DEBUG] No schema_data found in database for blog {blog.id}")
        else:
            print(f"[DEBUG] ORM already has schema_data: {blog.schema_data[:100] if blog.schema_data else 'None'}...")

        # Get related blogs from same category (excluding current blog)
        related_blogs = []
        if blog.category_id:
            related_blogs = (Blog.query
                           .filter_by(category_id=blog.category_id, status=True)
                           .filter(Blog.id != blog.id)
                           .order_by(Blog.created_at.desc())
                           .limit(3)
                           .all())

        # If no related blogs from category, get latest blogs
        if not related_blogs:
            related_blogs = (Blog.query
                           .filter_by(status=True)
                           .filter(Blog.id != blog.id)
                           .order_by(Blog.created_at.desc())
                           .limit(3)
                           .all())

        return render_template('blog_detail.html',
                             blog=blog,
                             related_blogs=related_blogs)
    except Exception as e:
        app.logger.error(f"Error loading blog detail: {str(e)}")
        flash('Blog not found', 'danger')
        return redirect(url_for('public_blogs'))
# ==================== PUBLIC WEBSTORY ROUTES ====================

@app.route('/webstories')
def public_webstories():
    """Public web story listing page"""
    try:
        # Get filter parameters
        page = request.args.get('page', 1, type=int)
        per_page = 12  # Show 12 web stories per page

        # Base query - only active web stories
        query = WebStory.query.filter_by(status=True)

        # Order by latest first
        query = query.order_by(WebStory.created_at.desc())

        # Paginate results
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        webstories = pagination.items

        return render_template('webstories.html',
                             webstories=webstories,
                             pagination=pagination)
    except Exception as e:
        app.logger.error(f"Error loading web stories: {str(e)}")
        flash('Error loading web stories', 'danger')
        return redirect(url_for('index'))

@app.route('/webstory/<slug>')
def webstory_detail(slug):
    """Individual web story detail page"""
    try:
        # Get web story by slug
        webstory = WebStory.query.filter_by(slug=slug, status=True).first_or_404()

        # Get related web stories (excluding current one)
        related_webstories = (WebStory.query
                           .filter_by(status=True)
                           .filter(WebStory.id != webstory.id)
                           .order_by(WebStory.created_at.desc())
                           .limit(6)
                           .all())

        return render_template('webstory_detail.html',
                             webstory=webstory,
                             related_webstories=related_webstories)
    except Exception as e:
        app.logger.error(f"Error loading web story detail: {str(e)}")
        flash('Web story not found', 'danger')
        return redirect(url_for('public_webstories'))


# Enhanced set_timezone route with better validation
@app.route('/set_timezone', methods=['POST'])
def set_timezone():
    """Store user's timezone in session with enhanced validation."""
    if request.is_json:
        timezone_data = request.get_json()
        timezone = timezone_data.get('timezone')
        
        if not timezone:
            return jsonify({'status': 'error', 'message': 'No timezone provided'}), 400
        
        # Validate timezone
        valid_timezone = False
        try:
            import pytz
            pytz.timezone(timezone)
            valid_timezone = True
        except (ImportError, pytz.exceptions.UnknownTimeZoneError):
            # Check against common timezones as fallback
            common_timezones = [
                'UTC', 'Asia/Kolkata', 'Asia/Calcutta', 'America/New_York', 
                'America/Los_Angeles', 'Europe/London', 'Europe/Paris',
                'Asia/Tokyo', 'Australia/Sydney'
            ]
            if timezone in common_timezones:
                valid_timezone = True
        
        if valid_timezone:
            session['user_timezone'] = timezone
            app.logger.info(f"Timezone set to: {timezone}")
            return jsonify({'status': 'success', 'timezone': timezone})
        else:
            # Set a reasonable default based on common patterns
            default_timezone = 'UTC'
            if 'Asia' in timezone or 'India' in timezone:
                default_timezone = 'Asia/Kolkata'
            elif 'America' in timezone:
                default_timezone = 'America/New_York'
            elif 'Europe' in timezone:
                default_timezone = 'Europe/London'
            
            session['user_timezone'] = default_timezone
            app.logger.warning(f"Invalid timezone {timezone}, set to default: {default_timezone}")
            return jsonify({'status': 'fallback', 'timezone': default_timezone})
    
    return jsonify({'status': 'error', 'message': 'Invalid request format'}), 400

def get_localized_datetime(utc_datetime, user_timezone=None):
    """
    Convert UTC datetime to user's local timezone with robust error handling.
    
    Args:
        utc_datetime: A datetime object in UTC
        user_timezone: Timezone string (e.g. 'America/New_York', 'Asia/Kolkata')
                      If None, uses session timezone or app default
    
    Returns:
        Datetime object converted to local timezone, or original datetime if conversion fails
    """
    if utc_datetime is None:
        return None
        
    try:
        # Ensure datetime has UTC timezone if not already set
        if utc_datetime.tzinfo is None:
            try:
                # For Python 3.11+
                from datetime import UTC
                utc_datetime = utc_datetime.replace(tzinfo=UTC)
            except ImportError:
                # For older Python versions
                from datetime import timezone
                utc_datetime = utc_datetime.replace(tzinfo=timezone.utc)
        
        # Get target timezone
        if user_timezone is None:
            user_timezone = session.get('user_timezone', app.config.get('DEFAULT_TIMEZONE', 'UTC'))
        
        # Handle UTC timezone specifically
        if user_timezone.upper() == 'UTC':
            return utc_datetime
        
        # Try using pytz if available
        try:
            import pytz
            target_tz = pytz.timezone(user_timezone)
            localized_dt = utc_datetime.astimezone(target_tz)
            return localized_dt
        except (ImportError, pytz.exceptions.UnknownTimeZoneError):
            # Fallback for common timezones without pytz
            from datetime import timedelta, timezone
            
            # Define common timezone offsets
            timezone_offsets = {
                'Asia/Kolkata': timedelta(hours=5, minutes=30),
                'Asia/Calcutta': timedelta(hours=5, minutes=30),
                'America/New_York': timedelta(hours=-5),  # EST (should adjust for DST)
                'America/Los_Angeles': timedelta(hours=-8),  # PST
                'Europe/London': timedelta(hours=0),  # GMT
                'Europe/Paris': timedelta(hours=1),  # CET
                'Asia/Tokyo': timedelta(hours=9),  # JST
                'Australia/Sydney': timedelta(hours=10),  # AEST
                'America/Chicago': timedelta(hours=-6),  # CST
                'America/Denver': timedelta(hours=-7),  # MST
                'Asia/Dubai': timedelta(hours=4),  # GST
                'Asia/Shanghai': timedelta(hours=8),  # CST
            }
            
            offset = timezone_offsets.get(user_timezone)
            if offset:
                target_tz = timezone(offset)
                return utc_datetime.astimezone(target_tz)
            
            # If timezone not found in our fallback list, try to parse it
            if '/' in user_timezone:
                # For zones like 'America/New_York', try to estimate offset
                parts = user_timezone.split('/')
                if len(parts) == 2:
                    continent, city = parts
                    # Very basic offset estimation (this is not DST-aware)
                    if continent == 'America':
                        if 'Los_Angeles' in city or 'Pacific' in city:
                            offset = timedelta(hours=-8)
                        elif 'Denver' in city or 'Mountain' in city:
                            offset = timedelta(hours=-7)
                        elif 'Chicago' in city or 'Central' in city:
                            offset = timedelta(hours=-6)
                        elif 'New_York' in city or 'Eastern' in city:
                            offset = timedelta(hours=-5)
                        else:
                            offset = timedelta(hours=-5)  # Default to EST
                    elif continent == 'Europe':
                        offset = timedelta(hours=1)  # Default to CET
                    elif continent == 'Asia':
                        if 'Tokyo' in city:
                            offset = timedelta(hours=9)
                        elif 'Shanghai' in city or 'Beijing' in city:
                            offset = timedelta(hours=8)
                        elif 'Dubai' in city:
                            offset = timedelta(hours=4)
                        elif 'Kolkata' in city or 'Calcutta' in city or 'Mumbai' in city:
                            offset = timedelta(hours=5, minutes=30)
                        else:
                            offset = timedelta(hours=5, minutes=30)  # Default to IST
                    else:
                        offset = timedelta(hours=0)  # Default to UTC
                        
                    if offset:
                        target_tz = timezone(offset)
                        return utc_datetime.astimezone(target_tz)
            
            # Final fallback: return UTC time
            app.logger.warning(f"Could not convert timezone {user_timezone}, returning UTC")
            return utc_datetime
            
    except Exception as e:
        # Log the error for debugging but don't fail
        app.logger.warning(f"Timezone conversion failed for {utc_datetime} to {user_timezone}: {str(e)}")
        return utc_datetime  # Return original UTC time as fallback


@app.template_filter('localize_datetime')
def localize_datetime_filter(value, format='%Y-%m-%d %H:%M:%S'):
    """
    Template filter to localize datetime to user's timezone
    
    Args:
        value: datetime object or string
        format: desired output format string
    
    Returns:
        Formatted datetime string in user's timezone
    """
    if value is None:
        return ''
    
    # If it's a string, try to parse it
    if isinstance(value, str):
        try:
            # Try parsing with multiple possible formats
            formats_to_try = [
                '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%dT%H:%M:%S',
                '%Y-%m-%d',
                '%Y-%m-%d %H:%M',
                '%Y-%m-%d %H:%M:%S.%f',
                '%Y-%m-%dT%H:%M:%S.%f',
            ]
            
            for fmt in formats_to_try:
                try:
                    value = datetime.strptime(value, fmt)
                    break
                except ValueError:
                    continue
            else:
                return value  # Return original string if no format matches
        except Exception:
            return value
    
    # If it's already a datetime object, localize and format it
    if isinstance(value, datetime):
        user_timezone = session.get('user_timezone', app.config.get('DEFAULT_TIMEZONE', 'UTC'))
        localized = get_localized_datetime(value, user_timezone)
        if localized:
            return localized.strftime(format)
        else:
            return value.strftime(format)
    
    return str(value)

    
def process_scan_timestamp(scan_timestamp, user_timezone=None):
    """
    Process a scan timestamp and return localized date and hour.
    
    Args:
        scan_timestamp: UTC timestamp from scan record
        user_timezone: User's timezone string
        
    Returns:
        tuple: (localized_date_string, localized_hour)
    """
    if not scan_timestamp:
        return 'Unknown', 0
        
    try:
        # Convert to user's timezone
        localized_time = get_localized_datetime(scan_timestamp, user_timezone)
        
        if localized_time:
            scan_date = localized_time.strftime('%Y-%m-%d')
            hour = localized_time.hour
            return scan_date, hour
        else:
            # Fallback to UTC
            scan_date = scan_timestamp.strftime('%Y-%m-%d')
            hour = scan_timestamp.hour
            return scan_date, hour
            
    except Exception as e:
        app.logger.warning(f"Error processing scan timestamp: {e}")
        # Fallback to UTC if conversion fails
        try:
            scan_date = scan_timestamp.strftime('%Y-%m-%d')
            hour = scan_timestamp.hour
            return scan_date, hour
        except:
            return 'Unknown', 0

@app.template_filter('tojson')
def to_json(value):
    import json
    try:
        if isinstance(value, str):
            return value
        return json.dumps(value)
    except:
        return value
    

@app.context_processor
def inject_subscription_data():
    """Make subscription data available to all templates"""
    if not session.get('user_id'):
        # Not logged in
        return {
            'has_subscription': False,
            'qr_remaining': 0,
            'analytics_remaining': 0,
            'scans_remaining': 0,
            'subscription_tier': 0,
            'can_create_dynamic': False,
            'available_designs': []
        }
    
    user_id = session.get('user_id')
    
    # Get active subscription data
    active_subscription = (
        SubscribedUser.query
        .filter(SubscribedUser.U_ID == user_id)
        .filter(SubscribedUser.end_date > datetime.now(UTC))
        .filter(SubscribedUser._is_active == True)
        .join(Subscription, SubscribedUser.S_ID == Subscription.S_ID)
        .first()
    )
    
    subscription_data = {
        'has_subscription': False,
        'qr_remaining': 0,
        'analytics_remaining': 0,
        'scans_remaining': 0,  
        'subscription_tier': 0,
        'can_create_dynamic': False,
        'available_designs': []
    }
    
    if active_subscription:
        # Calculate remaining QR codes
        qr_limit = active_subscription.effective_qr_limit
        qr_used = active_subscription.qr_generated
        qr_remaining = max(0, qr_limit - qr_used)

        # Calculate remaining analytics
        analytics_limit = active_subscription.effective_analytics_limit
        analytics_used = active_subscription.analytics_used
        analytics_remaining = max(0, analytics_limit - analytics_used)
        # Calculate remaining scans
        scans_remaining = active_subscription.get_scans_remaining()
        # Get subscription tier
        subscription_tier = active_subscription.subscription.tier
        
        # Check if user can create dynamic QR codes using improved logic
        plan_type_lower = active_subscription.subscription.plan_type.lower()
        plan_name_lower = active_subscription.subscription.plan.lower()

        can_create_dynamic = (
            plan_type_lower == 'dynamic' or
            'dynamic' in plan_name_lower or
            (plan_type_lower != 'normal' and subscription_tier >= 2)
        )
        
        # Get available designs
        available_designs = []
        if active_subscription.subscription.design:
            available_designs = active_subscription.subscription.get_designs()
        
        subscription_data.update({
            'has_subscription': True,
            'qr_remaining': qr_remaining,
            'analytics_remaining': analytics_remaining,
            'scans_remaining': scans_remaining,
            'subscription_tier': subscription_tier,
            'can_create_dynamic': can_create_dynamic,
            'available_designs': available_designs,
            'plan_name': active_subscription.subscription.plan,
            'days_remaining': active_subscription.days_remaining,
            'expires_on': active_subscription.end_date.strftime('%Y-%m-%d') if active_subscription.end_date else None
        })
    
    return subscription_data
@app.route('/qr_limits')
@login_required
def qr_limits():
    """Show current QR code generation limits and usage"""
    user_id = session.get('user_id')
    
    # Get active subscription
    active_subscription = (
        SubscribedUser.query
        .filter(SubscribedUser.U_ID == user_id)
        .filter(SubscribedUser.end_date > datetime.now(UTC))
        .filter(SubscribedUser._is_active == True)
        .join(Subscription, SubscribedUser.S_ID == Subscription.S_ID)
        .first()
    )
    
    if not active_subscription:
        flash('You need an active subscription to view QR code limits.', 'warning')
        return redirect(url_for('subscription.user_subscriptions'))
    
    # Get QR code counts
    total_qr_codes = QRCode.query.filter_by(user_id=user_id).count()
    
    # Count QR codes created today and this month
    today = datetime.now(UTC).date()
    today_start = datetime.combine(today, datetime.min.time()).replace(tzinfo=UTC)
    today_end = datetime.combine(today, datetime.max.time()).replace(tzinfo=UTC)
    
    # First day of current month
    month_start = datetime(today.year, today.month, 1, tzinfo=UTC)
    
    qr_created_today = QRCode.query.filter_by(user_id=user_id).filter(
        QRCode.created_at >= today_start,
        QRCode.created_at <= today_end
    ).count()
    
    qr_created_month = QRCode.query.filter_by(user_id=user_id).filter(
        QRCode.created_at >= month_start,
        QRCode.created_at <= today_end
    ).count()
    
    # Get subscription details
    subscription_plan = active_subscription.subscription
    
    # Calculate QR usage stats
    qr_limit = subscription_plan.qr_count
    qr_used = active_subscription.qr_generated
    qr_remaining = max(0, qr_limit - qr_used)
    qr_percent = (qr_used / qr_limit * 100) if qr_limit > 0 else 0
    
    # Calculate analytics usage stats
    analytics_limit = subscription_plan.analytics
    analytics_used = active_subscription.analytics_used
    analytics_remaining = max(0, analytics_limit - analytics_used)
    analytics_percent = (analytics_used / analytics_limit * 100) if analytics_limit > 0 else 0
    
    # Get available designs
    available_designs = []
    if subscription_plan.design:
        available_designs = subscription_plan.get_designs()
    
    # Get QR types by count
    qr_types = db.session.query(
        QRCode.qr_type, 
        func.count(QRCode.id).label('count')
    ).filter_by(user_id=user_id).group_by(QRCode.qr_type).all()
    
    return render_template('qr_limits.html',
                          subscription=active_subscription,
                          subscription_plan=subscription_plan,
                          total_qr_codes=total_qr_codes,
                          qr_created_today=qr_created_today,
                          qr_created_month=qr_created_month,
                          qr_used=qr_used,
                          qr_limit=qr_limit,
                          qr_remaining=qr_remaining,
                          qr_percent=qr_percent,
                          analytics_used=analytics_used,
                          analytics_limit=analytics_limit,
                          analytics_remaining=analytics_remaining,
                          analytics_percent=analytics_percent,
                          available_designs=available_designs,
                          qr_types=qr_types)

def get_subscription_tier(user_id):
    """Get the subscription tier for a user"""
    active_subscription = (
        SubscribedUser.query
        .filter(SubscribedUser.U_ID == user_id)
        .filter(SubscribedUser.end_date > datetime.now(UTC))
        .filter(SubscribedUser._is_active == True)
        .join(Subscription, SubscribedUser.S_ID == Subscription.S_ID)
        .first()
    )
    
    if active_subscription:
        return active_subscription.subscription.tier
    return 0

def can_create_dynamic_qr(user_id):
    """Check if a user can create dynamic QR codes based on subscription"""
    # Get active subscription
    active_subscription = (
        SubscribedUser.query
        .filter(SubscribedUser.U_ID == user_id)
        .filter(SubscribedUser.end_date > datetime.now(UTC))
        .filter(SubscribedUser._is_active == True)
        .join(Subscription, SubscribedUser.S_ID == Subscription.S_ID)
        .first()
    )

    if not active_subscription:
        return False

    subscription_plan = active_subscription.subscription
    plan_type_lower = subscription_plan.plan_type.lower()
    plan_name_lower = subscription_plan.plan.lower()

    # Allow dynamic QR codes if:
    # 1. Plan type is specifically "Dynamic" (case insensitive), OR
    # 2. Plan type is not "Normal" and tier >= 2, OR
    # 3. Plan name contains "dynamic" (case insensitive)
    return (
        plan_type_lower == 'dynamic' or
        'dynamic' in plan_name_lower or
        (plan_type_lower != 'normal' and subscription_plan.tier >= 2)
    )

def has_subscription_access(user_id, feature_type, tier_required=1):
    """
    Check if user has access to a specific feature based on subscription tier
    
    Args:
        user_id (int): User ID
        feature_type (str): Feature type ('dynamic', 'analytics', 'batch_export', etc.)
        tier_required (int): Minimum tier required for the feature
        
    Returns:
        bool: True if user has access, False otherwise
    """
    # Get user's subscription tier
    tier = get_subscription_tier(user_id)
    
    # Check tier requirement
    if tier < tier_required:
        return False
    
    # Get active subscription
    active_subscription = (
        SubscribedUser.query
        .filter(SubscribedUser.U_ID == user_id)
        .filter(SubscribedUser.end_date > datetime.now(UTC))
        .filter(SubscribedUser._is_active == True)
        .first()
    )
    
    if not active_subscription:
        return False
    
    # Check feature-specific limits
    if feature_type == 'analytics':
        # Check analytics limit
        return active_subscription.analytics_used < active_subscription.effective_analytics_limit
    elif feature_type == 'qr_code':
        # Check QR code limit
        return active_subscription.qr_generated < active_subscription.effective_qr_limit
    elif feature_type == 'dynamic':
        # Just check tier (already done above)
        return True
    elif feature_type == 'batch_export':
        # Check tier and analytics limit
        return (tier >= tier_required and
                active_subscription.analytics_used < active_subscription.effective_analytics_limit)
    
    # Default for unknown feature types
    return False

def check_subscription_access(route_function):
    """
    Decorator to redirect to subscription page when feature is unavailable
    
    Args:
        route_function (callable): Flask route function
        
    Returns:
        callable: Decorated function
    """
    @wraps(route_function)
    def decorated_function(*args, **kwargs):
        user_id = session.get('user_id')
        if not user_id:
            flash("You need to log in first.", "warning")
            return redirect(url_for('login'))
        
        # Check if user has an active subscription
        active_subscription = (
            SubscribedUser.query
            .filter(SubscribedUser.U_ID == user_id)
            .filter(SubscribedUser.end_date > datetime.now(UTC))
            .filter(SubscribedUser._is_active == True)
            .first()
        )
        
        if not active_subscription:
            flash("You need an active subscription to access this feature.", "warning")
            return redirect(url_for('subscription.user_subscriptions'))
            
        return route_function(*args, **kwargs)
        
    return decorated_function

def get_qr_options(qr_code):
    """Get QR code options from QR code model - ENHANCED with gradient + custom eyes support"""
    options = {}
    
    # Apply template if specified but create a copy to avoid modifying original template
    if qr_code.template and qr_code.template in QR_TEMPLATES:
        options.update(QR_TEMPLATES[qr_code.template].copy())
        print(f"Applied template: {qr_code.template}")
    
    # Basic styling - always override with user's explicit choices
    if qr_code.color and qr_code.color.strip() and qr_code.color != 'undefined' and qr_code.color != 'null':
        options['color'] = qr_code.color
        print(f"Set color from user choice: {qr_code.color}")
    
    if qr_code.background_color and qr_code.background_color.strip() and qr_code.background_color != 'undefined' and qr_code.background_color != 'null':
        options['background_color'] = qr_code.background_color
        print(f"Set background color from user choice: {qr_code.background_color}")
    
    if qr_code.shape:
        options['shape'] = qr_code.shape
    
    # SIMPLIFIED GRADIENT HANDLING - Use the dedicated gradient column
    using_gradient = qr_code.gradient  # Direct from database column
    options['using_gradient'] = using_gradient
    print(f"Gradient from database column: {using_gradient}")
    
    # If gradient is enabled, include gradient parameters
    if using_gradient:
        options['export_type'] = 'gradient'
        if qr_code.gradient_start:
            options['gradient_start'] = qr_code.gradient_start
        if qr_code.gradient_end:
            options['gradient_end'] = qr_code.gradient_end
        if qr_code.gradient_type:
            options['gradient_type'] = qr_code.gradient_type
        if qr_code.gradient_direction:
            options['gradient_direction'] = qr_code.gradient_direction
        print(f"Gradient colors: {qr_code.gradient_start} -> {qr_code.gradient_end}")
        
        # *** AUTO-ENABLE CUSTOM EYES FOR GRADIENT QR CODES ***
        options['custom_eyes'] = True
        print("Auto-enabled custom eyes for gradient QR code")
    else:
        # If not using gradient, ensure export type is PNG
        options['export_type'] = 'png'
        # Remove any gradient settings that might be in template
        for key in ['gradient_start', 'gradient_end', 'gradient_type', 'gradient_direction']:
            if key in options:
                del options[key]
        
        # For non-gradient, respect user's custom eyes setting
        options['custom_eyes'] = True if qr_code.custom_eyes is None else qr_code.custom_eyes
    
    # If custom eyes are enabled
    if options['custom_eyes']:
        # Set eye styles based on user choice or template
        if qr_code.inner_eye_style:
            options['inner_eye_style'] = qr_code.inner_eye_style
        elif 'inner_eye_style' not in options:
            # Default inner eye style based on shape
            shape_eye_map = {
                'circle': 'circle',
                'rounded': 'rounded', 
                'square': 'square',
                'vertical_bars': 'square',
                'horizontal_bars': 'square',
                'gapped_square': 'square'
            }
            qr_shape = options.get('shape', 'square')
            options['inner_eye_style'] = shape_eye_map.get(qr_shape, 'square')
            
        if qr_code.outer_eye_style:
            options['outer_eye_style'] = qr_code.outer_eye_style
        elif 'outer_eye_style' not in options:
            # Match outer style to inner if not specified
            if 'inner_eye_style' in options:
                options['outer_eye_style'] = options['inner_eye_style']
            else:
                shape_eye_map = {
                    'circle': 'circle',
                    'rounded': 'rounded',
                    'square': 'square'
                }
                qr_shape = options.get('shape', 'square')
                options['outer_eye_style'] = shape_eye_map.get(qr_shape, 'square')
        
        # Set eye colors based on user choices, gradient, or main color
        main_color = options.get('color', '#000000')
        
        # Set inner eye color
        if qr_code.inner_eye_color and qr_code.inner_eye_color.strip() and qr_code.inner_eye_color != 'undefined' and qr_code.inner_eye_color != 'null':
            options['inner_eye_color'] = qr_code.inner_eye_color
        elif using_gradient and qr_code.gradient_start:
            # For gradient QRs, use gradient start color for inner eye
            options['inner_eye_color'] = qr_code.gradient_start
        else:
            # Default to main QR color
            options['inner_eye_color'] = main_color
            
        # Set outer eye color
        if qr_code.outer_eye_color and qr_code.outer_eye_color.strip() and qr_code.outer_eye_color != 'undefined' and qr_code.outer_eye_color != 'null':
            options['outer_eye_color'] = qr_code.outer_eye_color
        elif using_gradient and qr_code.gradient_end:
            # For gradient QRs, use gradient end color for outer eye
            options['outer_eye_color'] = qr_code.gradient_end
        else:
            # Default to main QR color
            options['outer_eye_color'] = main_color
    
    # Logo settings
    if qr_code.logo_path:
        # Try multiple possible locations for the logo
        logo_locations = [
            qr_code.logo_path,  # As stored
            os.path.join(app.config['UPLOAD_FOLDER'], qr_code.logo_path),  # With upload folder
            os.path.join('static', 'uploads', qr_code.logo_path),  # With static/uploads
            os.path.join('static', qr_code.logo_path)  # With static
        ]
        
        # Find first existing location
        found_logo = False
        for location in logo_locations:
            if os.path.exists(location):
                options['logo_path'] = location
                found_logo = True
                print(f"Found logo at: {location}")
                break
        
        if not found_logo:
            print(f"Warning: Logo path {qr_code.logo_path} not found in any expected location")
            options['logo_path'] = os.path.join(app.config['UPLOAD_FOLDER'], qr_code.logo_path)
        
        options['round_logo'] = qr_code.round_logo
        options['logo_size_percentage'] = qr_code.logo_size_percentage
    
    # Frame settings
    if qr_code.frame_type:
        options['frame_type'] = qr_code.frame_type
        options['frame_text'] = qr_code.frame_text
        options['frame_color'] = getattr(qr_code, 'frame_color', '#000000')
    
    # Module settings
    options['module_size'] = qr_code.module_size
    options['quiet_zone'] = qr_code.quiet_zone
    options['error_correction'] = qr_code.error_correction
    
    # Watermark
    if qr_code.watermark_text:
        options['watermark_text'] = qr_code.watermark_text
    
    return options

# 3. ADD this new function (completely new):

def handle_gradient_custom_eyes_relationship(request_form, using_gradient):
    """
    Handle the automatic relationship between gradient selection and custom eyes.
    When gradient is enabled, automatically enable custom eyes and set appropriate colors.
    """
    # Get the custom eyes setting
    custom_eyes = 'custom_eyes' in request_form and request_form.get('custom_eyes') == 'true'
    if 'using_custom_eyes' in request_form and request_form.get('using_custom_eyes') == 'true':
        custom_eyes = True
    
    # *** AUTOMATIC GRADIENT → CUSTOM EYES RELATIONSHIP ***
    if using_gradient:
        print("Gradient mode detected during creation - auto-enabling custom eyes")
        custom_eyes = True
        
        # Get gradient colors
        gradient_start = request_form.get('gradient_start', '#f97316')
        gradient_end = request_form.get('gradient_end', '#fbbf24')
        
        # Get main QR color as fallback
        main_color = request_form.get('color', '#000000')
        
        # Set eye colors to match gradient if not explicitly set by user
        inner_eye_color = request_form.get('inner_eye_color', '')
        outer_eye_color = request_form.get('outer_eye_color', '')
        
        # If eye colors are not set or are default values, use gradient colors
        if not inner_eye_color or inner_eye_color in ['', 'undefined', 'null', '#000000']:
            inner_eye_color = gradient_start
            print(f"Auto-set inner eye color to gradient start: {inner_eye_color}")
        
        if not outer_eye_color or outer_eye_color in ['', 'undefined', 'null', '#000000']:
            outer_eye_color = gradient_end
            print(f"Auto-set outer eye color to gradient end: {outer_eye_color}")
        
        # Set default eye styles for gradient mode
        inner_eye_style = request_form.get('inner_eye_style', 'circle')
        outer_eye_style = request_form.get('outer_eye_style', 'circle')
        
        return {
            'custom_eyes': custom_eyes,
            'inner_eye_color': inner_eye_color,
            'outer_eye_color': outer_eye_color,
            'inner_eye_style': inner_eye_style,
            'outer_eye_style': outer_eye_style
        }
    
    else:
        # For non-gradient mode, respect user's choices
        inner_eye_color = request_form.get('inner_eye_color', '')
        outer_eye_color = request_form.get('outer_eye_color', '')
        inner_eye_style = request_form.get('inner_eye_style', 'circle')
        outer_eye_style = request_form.get('outer_eye_style', 'rounded')
        
        return {
            'custom_eyes': custom_eyes,
            'inner_eye_color': inner_eye_color,
            'outer_eye_color': outer_eye_color,
            'inner_eye_style': inner_eye_style,
            'outer_eye_style': outer_eye_style
        }


# 4. ADD this new function (completely new):
# *** SUPPORTING FUNCTIONS FOR THE FIXED EDIT_QR ROUTE ***

def apply_template_with_gradient_support(qr_code, template_name):
    """
    Apply template settings to QR code with proper gradient, custom eyes, and frame support.
    FIXED: Properly clear frame settings for non-corporate templates.
    """
    if not template_name or template_name not in QR_TEMPLATES:
        return
    
    template = QR_TEMPLATES[template_name]
    print(f"Applying template: {template_name}")
    
    # CRITICAL FIX: Handle frame clearing properly
    # Always clear frame settings first, then apply template-specific ones
    qr_code.frame_type = None
    qr_code.frame_color = None
    qr_code.frame_text = None
    print("Cleared all frame settings")
    
    # Apply template settings
    for key, value in template.items():
        if hasattr(qr_code, key):
            # Special handling for None values (to clear settings)
            if value is None:
                setattr(qr_code, key, None)
                print(f"Cleared {key}")
            else:
                setattr(qr_code, key, value)
                print(f"Set {key} = {value}")
    
    # Special handling for gradient templates
    if template.get('gradient', False) or template.get('using_gradient', False):
        print(f"Template {template_name} is a gradient template - ensuring proper setup")
        
        # Force enable custom eyes for gradient templates
        qr_code.custom_eyes = True
        qr_code.gradient = True  # Set the gradient column
        
        # Ensure gradient colors are set
        if 'gradient_start' in template and template['gradient_start']:
            qr_code.gradient_start = template['gradient_start']
        if 'gradient_end' in template and template['gradient_end']:
            qr_code.gradient_end = template['gradient_end']
        
        # Set eye colors to match gradient
        if 'inner_eye_color' in template and template['inner_eye_color']:
            qr_code.inner_eye_color = template['inner_eye_color']
        elif qr_code.gradient_start:
            qr_code.inner_eye_color = qr_code.gradient_start
            
        if 'outer_eye_color' in template and template['outer_eye_color']:
            qr_code.outer_eye_color = template['outer_eye_color']
        elif qr_code.gradient_end:
            qr_code.outer_eye_color = qr_code.gradient_end
        
        print(f"Gradient template setup complete: {qr_code.gradient_start} → {qr_code.gradient_end}")
    
    else:
        # For non-gradient templates, ensure gradient is disabled
        qr_code.gradient = False
        qr_code.export_type = 'png'
        # Clear gradient parameters
        qr_code.gradient_start = None
        qr_code.gradient_end = None
        qr_code.gradient_type = None
        qr_code.gradient_direction = None
        print(f"Non-gradient template - disabled gradient features")

def debug_upload_configuration():
    """Debug function to check upload folder configuration"""
    print("=== UPLOAD CONFIGURATION DEBUG ===")
    
    upload_folder = app.config.get('UPLOAD_FOLDER', 'static/uploads')
    print(f"UPLOAD_FOLDER config: {upload_folder}")
    print(f"Absolute path: {os.path.abspath(upload_folder)}")
    print(f"Directory exists: {os.path.exists(upload_folder)}")
    print(f"Is directory: {os.path.isdir(upload_folder)}")
    
    if os.path.exists(upload_folder):
        print(f"Directory writable: {os.access(upload_folder, os.W_OK)}")
        print(f"Directory contents: {os.listdir(upload_folder)}")
    
    logos_dir = os.path.join(upload_folder, 'logos')
    print(f"Logos directory: {logos_dir}")
    print(f"Logos dir exists: {os.path.exists(logos_dir)}")
    
    if os.path.exists(logos_dir):
        print(f"Logos dir writable: {os.access(logos_dir, os.W_OK)}")
        try:
            logos_files = os.listdir(logos_dir)
            print(f"Logo files: {logos_files}")
        except Exception as e:
            print(f"Error listing logos: {e}")
    
    print("=== END DEBUG ===")


# @app.route('/debug/uploads')
# @login_required
# def debug_uploads():
#     """Debug route to check upload configuration"""
#     debug_upload_configuration()
    
#     return jsonify({
#         'upload_folder': app.config.get('UPLOAD_FOLDER', 'static/uploads'),
#         'upload_folder_exists': os.path.exists(app.config.get('UPLOAD_FOLDER', 'static/uploads')),
#         'current_working_directory': os.getcwd(),
#         'absolute_upload_path': os.path.abspath(app.config.get('UPLOAD_FOLDER', 'static/uploads'))
#     })

def safe_getattr(obj, attr, default=None):
    """Safely get attribute from object with fallback"""
    try:
        return getattr(obj, attr, default)
    except (AttributeError, TypeError):
        return default

def safe_form_data_preparation(qr_code):
    """Safely prepare form data with comprehensive error handling"""
    try:
        app.logger.info(f"Preparing form data for QR {qr_code.unique_id}")
        
        # Get basic attributes with safety checks
        gradient_enabled = safe_getattr(qr_code, 'gradient', False)
        main_color = safe_getattr(qr_code, 'color') or '#000000'
        bg_color = safe_getattr(qr_code, 'background_color') or '#FFFFFF'
        
        # Prepare form data with comprehensive defaults
        form_data = {
            # Gradient settings
            'using_gradient': gradient_enabled,
            'gradient_start': safe_getattr(qr_code, 'gradient_start') or '#f97316',
            'gradient_end': safe_getattr(qr_code, 'gradient_end') or '#fbbf24',
            'gradient_type': safe_getattr(qr_code, 'gradient_type') or 'linear',
            'gradient_direction': safe_getattr(qr_code, 'gradient_direction') or 'to-right',
            
            # Eye customization
            'custom_eyes': safe_getattr(qr_code, 'custom_eyes', False),
            'inner_eye_style': safe_getattr(qr_code, 'inner_eye_style') or 'circle',
            'outer_eye_style': safe_getattr(qr_code, 'outer_eye_style') or 'rounded',
            'inner_eye_color': safe_getattr(qr_code, 'inner_eye_color') or main_color,
            'outer_eye_color': safe_getattr(qr_code, 'outer_eye_color') or main_color,
            
            # Export settings
            'export_type': 'gradient' if gradient_enabled else 'png',
            
            # Frame settings
            'frame_type': safe_getattr(qr_code, 'frame_type') or '',
            'frame_text': safe_getattr(qr_code, 'frame_text') or '',
            'frame_color': safe_getattr(qr_code, 'frame_color') or '#000000',
            
            # Basic styling
            'color': main_color,
            'background_color': bg_color,
            'shape': safe_getattr(qr_code, 'shape') or 'square',
            'template': safe_getattr(qr_code, 'template') or '',
            
            # Advanced settings
            'module_size': safe_getattr(qr_code, 'module_size') or 10,
            'quiet_zone': safe_getattr(qr_code, 'quiet_zone') or 4,
            'error_correction': safe_getattr(qr_code, 'error_correction') or 'H',
            'logo_size_percentage': safe_getattr(qr_code, 'logo_size_percentage') or 25,
            'round_logo': safe_getattr(qr_code, 'round_logo', False),
            'watermark_text': safe_getattr(qr_code, 'watermark_text') or ''
        }
        
        app.logger.info(f"Form data prepared: gradient={form_data['using_gradient']}, custom_eyes={form_data['custom_eyes']}")
        return form_data
        
    except Exception as e:
        app.logger.error(f"Error preparing form data: {str(e)}")
        # Return minimal safe defaults if anything fails
        return {
            'using_gradient': False,
            'gradient_start': '#f97316',
            'gradient_end': '#fbbf24',
            'gradient_type': 'linear',
            'gradient_direction': 'to-right',
            'custom_eyes': False,
            'inner_eye_style': 'circle',
            'outer_eye_style': 'rounded',
            'inner_eye_color': '#000000',
            'outer_eye_color': '#000000',
            'export_type': 'png',
            'frame_type': '',
            'frame_text': '',
            'frame_color': '#000000',
            'color': '#000000',
            'background_color': '#FFFFFF',
            'shape': 'square',
            'template': '',
            'module_size': 10,
            'quiet_zone': 4,
            'error_correction': 'H',
            'logo_size_percentage': 25,
            'round_logo': False,
            'watermark_text': ''
        }

def safe_content_parsing(qr_code):
    """Safely parse QR code content with error handling"""
    content = {}
    try:
        if qr_code.content:
            content = json.loads(qr_code.content)
            app.logger.info(f"Successfully parsed QR content for {qr_code.unique_id}")
        else:
            app.logger.warning(f"QR code {qr_code.unique_id} has empty content")
    except (json.JSONDecodeError, TypeError) as e:
        app.logger.error(f"Failed to parse QR content for {qr_code.unique_id}: {str(e)}")
        content = {}
    return content

def safe_qr_generation(qr_code):
    """Safely generate QR code with error handling and fallback"""
    try:
        qr_image, qr_info = generate_qr_code(qr_code)
        app.logger.info(f"QR code generated successfully for {qr_code.unique_id}")
        return qr_image, qr_info
    except Exception as e:
        app.logger.error(f"QR generation failed for {qr_code.unique_id}: {str(e)}")
        # Return placeholder image
        placeholder_image = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        error_info = {'error': str(e), 'fallback': True}
        return placeholder_image, error_info

def safe_subscription_check(user_id):
    """Safely check user subscription with error handling"""
    try:
        active_subscription = (
            SubscribedUser.query
            .filter(SubscribedUser.U_ID == user_id)
            .filter(SubscribedUser.end_date > datetime.now(UTC))
            .filter(SubscribedUser._is_active == True)
            .first()
        )
        
        available_templates = []
        if active_subscription:
            try:
                if hasattr(active_subscription, 'subscription') and active_subscription.subscription.design:
                    available_templates = active_subscription.subscription.get_designs()
                    app.logger.info(f"Found {len(available_templates)} available templates for user {user_id}")
            except Exception as e:
                app.logger.warning(f"Failed to get available designs for user {user_id}: {str(e)}")
                available_templates = []
        
        return active_subscription, available_templates
        
    except Exception as e:
        app.logger.warning(f"Failed to get subscription info for user {user_id}: {str(e)}")
        return None, []

def safe_scan_retrieval(qr_code_id):
    """Safely retrieve scans with error handling"""
    try:
        scans = Scan.query.filter_by(qr_code_id=qr_code_id).order_by(Scan.timestamp.desc()).limit(50).all()
        app.logger.info(f"Found {len(scans)} scans for QR code {qr_code_id}")
        return scans
    except Exception as e:
        app.logger.error(f"Failed to fetch scans for QR code {qr_code_id}: {str(e)}")
        return []
    
# ==================================================================
# COMPLETE UPDATE_QR_CONTENT_BY_TYPE FUNCTION - Add to your edit_qr route
# ==================================================================

def update_qr_content_by_type(qr_code, form_data):
    """Update QR code content based on type with comprehensive error handling"""
    content_updated = False
    qr_type = qr_code.qr_type
    
    try:
        app.logger.info(f"Updating content for QR type: {qr_type}")
        
        if qr_type == 'link':
            link_detail = qr_code.link_detail
            new_url = form_data.get('url', '')
            if link_detail and new_url and new_url != link_detail.url:
                link_detail.url = new_url
                content_updated = True
                app.logger.info(f"Updated link URL to: {new_url}")
            elif not link_detail and new_url:
                link_detail = QRLink(qr_code_id=qr_code.id, url=new_url)
                db.session.add(link_detail)
                content_updated = True
                
        elif qr_type == 'email':
            email_detail = qr_code.email_detail
            new_email = form_data.get('email', '')
            new_subject = form_data.get('subject', '')
            new_body = form_data.get('body', '')
            
            if email_detail:
                if (new_email != email_detail.email or 
                    new_subject != (email_detail.subject or '') or 
                    new_body != (email_detail.body or '')):
                    email_detail.email = new_email
                    email_detail.subject = new_subject
                    email_detail.body = new_body
                    content_updated = True
                    app.logger.info("Updated email details")
            elif new_email:
                email_detail = QREmail(
                    qr_code_id=qr_code.id,
                    email=new_email,
                    subject=new_subject,
                    body=new_body
                )
                db.session.add(email_detail)
                content_updated = True
                
        elif qr_type == 'text':
            text_detail = qr_code.text_detail
            new_text = form_data.get('text', '')
            if text_detail and new_text != text_detail.text:
                text_detail.text = new_text
                content_updated = True
                app.logger.info("Updated text content")
            elif not text_detail and new_text:
                text_detail = QRText(qr_code_id=qr_code.id, text=new_text)
                db.session.add(text_detail)
                content_updated = True
                
        elif qr_type == 'call':
            phone_detail = qr_code.phone_detail
            new_phone = form_data.get('phone', '')
            if phone_detail and new_phone != phone_detail.phone:
                phone_detail.phone = new_phone
                content_updated = True
                app.logger.info("Updated phone number")
            elif not phone_detail and new_phone:
                phone_detail = QRPhone(qr_code_id=qr_code.id, phone=new_phone)
                db.session.add(phone_detail)
                content_updated = True
                
        elif qr_type == 'sms':
            sms_detail = qr_code.sms_detail
            new_phone = form_data.get('sms-phone') or form_data.get('phone', '')
            new_message = form_data.get('message', '')
            if sms_detail:
                if (new_phone != sms_detail.phone or 
                    new_message != (sms_detail.message or '')):
                    sms_detail.phone = new_phone
                    sms_detail.message = new_message
                    content_updated = True
                    app.logger.info("Updated SMS details")
            elif new_phone:
                sms_detail = QRSms(
                    qr_code_id=qr_code.id,
                    phone=new_phone,
                    message=new_message
                )
                db.session.add(sms_detail)
                content_updated = True
                
        elif qr_type == 'whatsapp':
            whatsapp_detail = qr_code.whatsapp_detail
            new_phone = form_data.get('whatsapp-phone') or form_data.get('phone', '')
            new_message = form_data.get('whatsapp-message') or form_data.get('message', '')
            if whatsapp_detail:
                if (new_phone != whatsapp_detail.phone or 
                    new_message != (whatsapp_detail.message or '')):
                    whatsapp_detail.phone = new_phone
                    whatsapp_detail.message = new_message
                    content_updated = True
                    app.logger.info("Updated WhatsApp details")
            elif new_phone:
                whatsapp_detail = QRWhatsApp(
                    qr_code_id=qr_code.id,
                    phone=new_phone,
                    message=new_message
                )
                db.session.add(whatsapp_detail)
                content_updated = True
                
        elif qr_type == 'wifi':
            # *** COMPLETE WIFI UPDATE HANDLING ***
            wifi_detail = qr_code.wifi_detail
            new_ssid = form_data.get('ssid', '')
            new_password = form_data.get('password', '')
            new_encryption = form_data.get('encryption', 'WPA')
            
            app.logger.info(f"Updating WiFi - SSID: {new_ssid}, Encryption: {new_encryption}")
            
            if wifi_detail:
                # Update existing WiFi detail
                if (new_ssid != wifi_detail.ssid or 
                    new_password != (wifi_detail.password or '') or 
                    new_encryption != wifi_detail.encryption):
                    
                    wifi_detail.ssid = new_ssid
                    wifi_detail.password = new_password
                    wifi_detail.encryption = new_encryption
                    content_updated = True
                    app.logger.info("Updated existing WiFi details")
            elif new_ssid:
                # Create new WiFi detail record
                wifi_detail = QRWifi(
                    qr_code_id=qr_code.id,
                    ssid=new_ssid,
                    password=new_password,
                    encryption=new_encryption
                )
                db.session.add(wifi_detail)
                content_updated = True
                app.logger.info("Created new WiFi detail record")
                
        elif qr_type == 'vcard':
            vcard_detail = qr_code.vcard_detail
            new_name = form_data.get('full_name', '')
            new_phone = form_data.get('vcard-phone') or form_data.get('phone', '')
            new_email = form_data.get('vcard-email') or form_data.get('email', '')
            new_company = form_data.get('company', '')
            new_title = form_data.get('title', '')
            new_address = form_data.get('address', '')
            new_website = form_data.get('website', '')
            
            # Handle social media data
            social_media_data = {}
            social_platforms = ['facebook', 'twitter', 'linkedin', 'instagram', 
                               'youtube', 'whatsapp', 'telegram', 'github', 
                               'tiktok', 'pinterest', 'snapchat', 'discord', 'reddit', 'tumblr']
            
            for platform in social_platforms:
                social_url = form_data.get(f'social_{platform}', '')
                if social_url:
                    social_media_data[platform] = social_url
            
            if vcard_detail:
                current_social = {}
                if vcard_detail.social_media:
                    try:
                        current_social = json.loads(vcard_detail.social_media)
                    except:
                        current_social = {}
                
                if (new_name != vcard_detail.name or 
                    new_phone != (vcard_detail.phone or '') or 
                    new_email != (vcard_detail.email or '') or 
                    new_company != (vcard_detail.company or '') or 
                    new_title != (vcard_detail.title or '') or 
                    new_address != (vcard_detail.address or '') or 
                    new_website != (vcard_detail.website or '') or
                    social_media_data != current_social):
                    
                    vcard_detail.name = new_name
                    vcard_detail.phone = new_phone
                    vcard_detail.email = new_email
                    vcard_detail.company = new_company
                    vcard_detail.title = new_title
                    vcard_detail.address = new_address
                    vcard_detail.website = new_website
                    vcard_detail.social_media = json.dumps(social_media_data) if social_media_data else None
                    content_updated = True
                    app.logger.info("Updated vCard details")
            elif new_name:
                vcard_detail = QRVCard(
                    qr_code_id=qr_code.id,
                    name=new_name,
                    phone=new_phone,
                    email=new_email,
                    company=new_company,
                    title=new_title,
                    address=new_address,
                    website=new_website,
                    social_media=json.dumps(social_media_data) if social_media_data else None
                )
                db.session.add(vcard_detail)
                content_updated = True
                
        elif qr_type == 'event':
            event_detail = qr_code.event_detail
            new_title = form_data.get('event-title') or form_data.get('title', '')
            new_location = form_data.get('location', '')
            new_description = form_data.get('description', '')
            new_organizer = form_data.get('organizer', '')
            
            # Handle datetime fields
            new_start_date = None
            new_end_time = None
            
            start_date_str = form_data.get('start_date', '')
            if start_date_str:
                try:
                    new_start_date = datetime.fromisoformat(start_date_str.replace('Z', '+00:00'))
                except ValueError:
                    try:
                        new_start_date = datetime.strptime(start_date_str, '%Y-%m-%dT%H:%M')
                    except ValueError:
                        pass
            
            end_time_str = form_data.get('end_time', '')
            if end_time_str:
                try:
                    new_end_time = datetime.fromisoformat(end_time_str.replace('Z', '+00:00'))
                except ValueError:
                    try:
                        new_end_time = datetime.strptime(end_time_str, '%Y-%m-%dT%H:%M')
                    except ValueError:
                        pass
            
            if event_detail:
                if (new_title != event_detail.title or 
                    new_location != (event_detail.location or '') or 
                    new_description != (event_detail.description or '') or 
                    new_organizer != (event_detail.organizer or '') or
                    new_start_date != event_detail.start_date or
                    new_end_time != event_detail.end_time):
                    
                    event_detail.title = new_title
                    event_detail.location = new_location
                    event_detail.description = new_description
                    event_detail.organizer = new_organizer
                    if new_start_date:
                        event_detail.start_date = new_start_date
                    if new_end_time:
                        event_detail.end_time = new_end_time
                    content_updated = True
                    app.logger.info("Updated event details")
            elif new_title:
                event_detail = QREvent(
                    qr_code_id=qr_code.id,
                    title=new_title,
                    location=new_location,
                    description=new_description,
                    organizer=new_organizer,
                    start_date=new_start_date,
                    end_time=new_end_time
                )
                db.session.add(event_detail)
                content_updated = True

        elif qr_type == 'image':
            image_detail = qr_code.image_detail
            new_caption = form_data.get('image_caption', '')
            new_bg_color_1 = form_data.get('img_bg_color_1', '#0a0a0a')
            new_bg_color_2 = form_data.get('img_bg_color_2', '#1a1a2e')
            new_bg_direction = form_data.get('img_bg_direction', 'to bottom')

            # Check if a new image file is uploaded
            new_image_file = request.files.get('qr_image_file') if hasattr(request, 'files') else None

            if new_image_file and new_image_file.filename:
                import uuid as uuid_module
                allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
                file_ext = new_image_file.filename.rsplit('.', 1)[-1].lower() if '.' in new_image_file.filename else ''
                if file_ext in allowed_extensions:
                    # Delete old image
                    if image_detail and image_detail.image_path:
                        old_path = os.path.join(app.config['UPLOAD_FOLDER'], image_detail.image_path)
                        if os.path.exists(old_path):
                            try:
                                os.remove(old_path)
                            except:
                                pass
                    image_filename = f"{uuid_module.uuid4().hex}_{new_image_file.filename}".replace(' ', '_')
                    image_save_path = os.path.join(app.config['UPLOAD_FOLDER'], 'qr_images', image_filename)
                    new_image_file.save(image_save_path)
                    new_image_path = os.path.join('qr_images', image_filename).replace('\\', '/')

                    if image_detail:
                        image_detail.image_path = new_image_path
                        image_detail.caption = new_caption
                        image_detail.bg_color_1 = new_bg_color_1
                        image_detail.bg_color_2 = new_bg_color_2
                        image_detail.bg_direction = new_bg_direction
                    else:
                        image_detail = QRImage(
                            qr_code_id=qr_code.id,
                            image_path=new_image_path,
                            caption=new_caption,
                            bg_color_1=new_bg_color_1,
                            bg_color_2=new_bg_color_2,
                            bg_direction=new_bg_direction
                        )
                        db.session.add(image_detail)
                    content_updated = True
            elif image_detail:
                changed = False
                if new_caption != (image_detail.caption or ''):
                    image_detail.caption = new_caption
                    changed = True
                if new_bg_color_1 != (image_detail.bg_color_1 or '#0a0a0a'):
                    image_detail.bg_color_1 = new_bg_color_1
                    changed = True
                if new_bg_color_2 != (image_detail.bg_color_2 or '#1a1a2e'):
                    image_detail.bg_color_2 = new_bg_color_2
                    changed = True
                if new_bg_direction != (image_detail.bg_direction or 'to bottom'):
                    image_detail.bg_direction = new_bg_direction
                    changed = True
                if changed:
                    content_updated = True

        app.logger.info(f"Content update completed for {qr_type}, changed: {content_updated}")
        return content_updated
        
    except Exception as e:
        app.logger.error(f"Error updating content for QR type {qr_type}: {str(e)}")
        return False

@app.route('/debug/schema-check')
@login_required
def debug_schema_check():
    """Check database schema for missing columns"""
    try:
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        columns = [col['name'] for col in inspector.get_columns('qr_code')]
        
        required_columns = [
            'id', 'unique_id', 'name', 'qr_type', 'is_dynamic', 'content',
            'created_at', 'updated_at', 'color', 'background_color', 'logo_path',
            'frame_type', 'shape', 'template', 'custom_eyes', 'inner_eye_style',
            'outer_eye_style', 'inner_eye_color', 'outer_eye_color', 'module_size',
            'quiet_zone', 'error_correction', 'gradient', 'gradient_start',
            'gradient_end', 'export_type', 'watermark_text', 'logo_size_percentage',
            'round_logo', 'frame_text', 'gradient_type', 'gradient_direction',
            'frame_color', 'user_id'
        ]
        
        missing_columns = [col for col in required_columns if col not in columns]
        
        return jsonify({
            'total_columns_found': len(columns),
            'total_columns_required': len(required_columns),
            'missing_columns': missing_columns,
            'needs_migration': len(missing_columns) > 0,
            'columns_found': sorted(columns),
            'status': 'MISSING_COLUMNS' if missing_columns else 'SCHEMA_OK'
        })
        
    except Exception as e:
        return jsonify({
            'error': str(e),
            'status': 'SCHEMA_CHECK_FAILED'
        })    

# ==================================================================
# COMPLETE DEBUG FUNCTION - Add this for testing WiFi QR codes
# ==================================================================

@app.route('/debug/wifi/<qr_id>')
@login_required  
def debug_wifi(qr_id):
    """Debug WiFi QR code data"""
    qr_code = QRCode.query.filter_by(unique_id=qr_id).first_or_404()
    
    if qr_code.user_id != current_user.id:
        return jsonify({'error': 'Access denied'}), 403
    
    wifi_detail = qr_code.wifi_detail
    content = json.loads(qr_code.content) if qr_code.content else {}
    
    debug_info = {
        'qr_id': qr_id,
        'qr_type': qr_code.qr_type,
        'is_dynamic': qr_code.is_dynamic,
        'has_wifi_detail': wifi_detail is not None,
        'wifi_detail': {
            'ssid': wifi_detail.ssid if wifi_detail else None,
            'password_exists': bool(wifi_detail.password) if wifi_detail else None,
            'encryption': wifi_detail.encryption if wifi_detail else None
        } if wifi_detail else None,
        'content_json': content,
        'generated_qr_string': generate_qr_data(qr_code),
        'redirect_url': url_for('redirect_qr', qr_id=qr_id, _external=True)
    }
    
    return jsonify(debug_info)
@app.route('/contact', methods=['GET', 'POST'])
def contact():
    if request.method == 'POST':
        try:
            # Get form data
            name = request.form.get('name')
            email = request.form.get('email')
            message = request.form.get('message')

            # Verify reCAPTCHA
            recaptcha_response = request.form.get('g-recaptcha-response')
            if not recaptcha_response:
                flash('Please complete the reCAPTCHA verification.', 'warning')
                return render_template('contact.html', name=name, email=email, message=message)

            # Verify with Google
            recaptcha_secret = os.getenv('RECAPTCHA_SECRET_KEY', '6LcjljssAAAAAFEZpDSSgtugawtx6-wjpAnKyHeG')
            recaptcha_verify_url = 'https://www.google.com/recaptcha/api/siteverify'
            recaptcha_data = {
                'secret': recaptcha_secret,
                'response': recaptcha_response,
                'remoteip': request.remote_addr
            }
            recaptcha_result = requests.post(recaptcha_verify_url, data=recaptcha_data)
            recaptcha_result_json = recaptcha_result.json()

            if not recaptcha_result_json.get('success'):
                flash('reCAPTCHA verification failed. Please try again.', 'danger')
                return render_template('contact.html', name=name, email=email, message=message)

            # Validate required fields
            if not all([name, email, message]):
                flash('Please fill in all required fields.', 'warning')
                return render_template('contact.html')
            
            # ✅ SAVE TO DATABASE FIRST
            contact_submission = ContactSubmission(
                name=name,
                email=email,
                message=message,
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent', '')
            )
            
            db.session.add(contact_submission)
            db.session.commit()
            
            # Send email notification to support team
            subject = f"QR Dada Contact Form: {name}"
            body = f"""Contact Form Submission (ID: {contact_submission.id}):

Name: {name}
Email: {email}
IP Address: {request.remote_addr}
Submitted: {contact_submission.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}

Message:
{message}
"""

            # Send to support team
            email_service.send_support_email(
                to='support@qrdada.com',
                subject=subject,
                body=body
            )

            # Send auto-reply to customer
            auto_reply_subject = "Thank you for contacting QR Dada"
            auto_reply_body = f"""Dear {name},

Thank you for contacting QR Dada. We have received your message (Reference ID: {contact_submission.id}) and will get back to you as soon as possible, typically within 24 hours during business days.

For urgent inquiries, please call our support line at +1 (800) 123-4567.

Best Regards,
The QR Dada Support Team
"""

            email_service.send_support_email(
                to=email,
                subject=auto_reply_subject,
                body=auto_reply_body
            )
            
            flash('contact:Your message has been sent successfully! We will contact you soon.', 'success')
            return redirect(url_for('contact'))
            
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error processing contact form: {str(e)}")
            flash('There was an error sending your message. Please try again later.', 'danger')
            
    return render_template('contact.html')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')
@app.route('/terms')
def terms():
    return render_template('terms.html')
@app.route('/about')
def about():
    return render_template('about.html')
@app.route('/cookie-policy')
def cookie_policy():
    return render_template('cookie_policy.html')

@app.route('/time-date')
def time_and_date_today():
    current_time = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    return jsonify({"current_time": current_time})

# ==================================================================
# ADDITIONAL HELPER FUNCTION FOR WIFI VALIDATION
# ==================================================================

def validate_wifi_data(ssid, password, encryption):
    """Validate WiFi QR code data"""
    errors = []
    
    if not ssid or not ssid.strip():
        errors.append("SSID (Network Name) is required")
    elif len(ssid) > 32:
        errors.append("SSID cannot be longer than 32 characters")
    
    if encryption not in ['WPA', 'WPA2', 'WEP', 'nopass']:
        errors.append("Invalid encryption type")
    
    if encryption != 'nopass' and not password:
        errors.append("Password is required for secured networks")
    elif password and len(password) < 8 and encryption in ['WPA', 'WPA2']:
        errors.append("WPA/WPA2 password must be at least 8 characters")
    
    return errors


def sync_all_subscription_data():
    """
    One-time sync script to fix all existing subscription data
    Run this once after implementing the new analytics tracking
    """
    try:
        print("Starting data sync for all subscriptions...")
        
        # Get all active subscriptions
        active_subscriptions = (
            SubscribedUser.query
            .filter(SubscribedUser.end_date > datetime.now(UTC))
            .filter(SubscribedUser._is_active == True)
            .all()
        )
        
        print(f"Found {len(active_subscriptions)} active subscriptions to sync")
        
        for subscription in active_subscriptions:
            user_id = subscription.U_ID
            print(f"\nSyncing user {user_id} subscription {subscription.id}")
            
            # 1. Sync QR code count for subscription period
            subscription_start = subscription.start_date
            subscription_end = subscription.end_date
            
            qr_count_in_period = QRCode.query.filter(
                QRCode.user_id == user_id,
                QRCode.created_at >= subscription_start,
                QRCode.created_at <= subscription_end
            ).count()
            
            old_qr_count = subscription.qr_generated
            subscription.qr_generated = qr_count_in_period
            print(f"  QR count: {old_qr_count} -> {qr_count_in_period}")
            
            # 2. Sync analytics count (scans of dynamic QR codes during subscription period)
            dynamic_qr_codes = QRCode.query.filter(
                QRCode.user_id == user_id,
                QRCode.is_dynamic == True  # Adjust this condition based on your model
            ).all()
            
            total_analytics_in_period = 0
            for qr_code in dynamic_qr_codes:
                scans_in_period = Scan.query.filter(
                    Scan.qr_code_id == qr_code.id,
                    Scan.timestamp >= subscription_start,
                    Scan.timestamp <= subscription_end
                ).count()
                total_analytics_in_period += scans_in_period
            
            old_analytics_count = subscription.analytics_used
            subscription.analytics_used = total_analytics_in_period
            print(f"  Analytics count: {old_analytics_count} -> {total_analytics_in_period}")
            
        # Commit all changes
        db.session.commit()
        print(f"\nData sync completed successfully for {len(active_subscriptions)} subscriptions!")
        
        return True
        
    except Exception as e:
        print(f"Error during data sync: {e}")
        db.session.rollback()
        return False
@app.route('/pricing')
def pricing():
    # Get all active subscription plans
    subscriptions = Subscription.query.filter_by(is_active=True).all()

    return render_template('pricing.html', subscriptions=subscriptions)

# Route to run the sync (add this to your app.py temporarily)
@app.route('/admin/sync-subscription-data')
@login_required
def sync_subscription_data():
    """
    Admin route to sync subscription data
    Remove this route after running once
    """
    # Add admin check here if needed
    if current_user.id != 1:  # Assuming user ID 1 is admin
        return "Unauthorized", 403
    
    result = sync_all_subscription_data()
    
    if result:
        return "Data sync completed successfully!"
    else:
        return "Data sync failed! Check logs for details."

# Alternative: Management command version
def run_sync_command():
    """
    Run this as a separate Python script or Flask command
    """
    # from your_app import app  # Import your Flask app

    with app.app_context():
        sync_all_subscription_data()


@app.route('/sitemap')
def sitemap_page():
    """Display sitemap categories page"""
    blogs_count = 0
    webstories_count = 0
    website_settings = None
    pages_count = 19  # Main pages + services + legal pages

    try:
        blogs_count = Blog.query.filter_by(status=True).count()
    except Exception as e:
        print(f"Error fetching blogs count: {e}")

    try:
        webstories_count = WebStory.query.filter_by(status=True).count()
    except Exception as e:
        print(f"Error fetching webstories count: {e}")

    try:
        website_settings = WebsiteSettings.query.first()
    except Exception as e:
        print(f"Error fetching website settings: {e}")

    return render_template(
        'sitemap_index.html',
        blogs_count=blogs_count,
        webstories_count=webstories_count,
        pages_count=pages_count,
        website_settings=website_settings
    )


@app.route('/page-sitemap')
def sitemap_pages():
    """Display all website pages"""
    website_settings = None
    base_url = request.url_root.rstrip('/')
    current_date = datetime.now().strftime('%b %d, %Y')

    try:
        website_settings = WebsiteSettings.query.first()
    except Exception as e:
        print(f"Error fetching website settings: {e}")

    return render_template(
        'sitemap_pages.html',
        website_settings=website_settings,
        base_url=base_url,
        current_date=current_date
    )


@app.route('/post-sitemap')
def sitemap_blogs():
    """Display all blog articles"""
    import re
    blogs = []
    website_settings = None
    base_url = request.url_root.rstrip('/')
    current_date = datetime.now().strftime('%b %d, %Y')

    try:
        blogs = Blog.query.filter_by(status=True).order_by(Blog.updated_at.desc()).all()
        # Calculate image count for each blog
        for blog in blogs:
            image_count = 0
            # Count cover image
            if blog.image:
                image_count += 1
            # Count images in description HTML
            if blog.description:
                img_tags = re.findall(r'<img[^>]+>', blog.description, re.IGNORECASE)
                image_count += len(img_tags)
            blog.image_count = image_count
    except Exception as e:
        print(f"Error fetching blogs: {e}")

    try:
        website_settings = WebsiteSettings.query.first()
    except Exception as e:
        print(f"Error fetching website settings: {e}")

    return render_template(
        'sitemap_blogs.html',
        blogs=blogs,
        website_settings=website_settings,
        base_url=base_url,
        current_date=current_date
    )


@app.route('/web-story-sitemap')
def sitemap_webstories():
    """Display all web stories"""
    webstories = []
    website_settings = None
    base_url = request.url_root.rstrip('/')
    current_date = datetime.now().strftime('%b %d, %Y')

    try:
        webstories = WebStory.query.filter_by(status=True).order_by(WebStory.updated_at.desc()).all()
        # Calculate image count for each webstory
        for story in webstories:
            image_count = 0
            # Count cover image
            if story.cover_image:
                image_count += 1
            # Count images in slides
            if story.slides:
                for slide in story.slides:
                    if slide.get('image'):
                        image_count += 1
            story.image_count = image_count
    except Exception as e:
        print(f"Error fetching webstories: {e}")

    try:
        website_settings = WebsiteSettings.query.first()
    except Exception as e:
        print(f"Error fetching website settings: {e}")

    return render_template(
        'sitemap_webstories.html',
        webstories=webstories,
        website_settings=website_settings,
        base_url=base_url,
        current_date=current_date
    )


@app.route('/sitemap.xml')
@app.route('/sitemap_index.xml')
def sitemap_xml():
    """Generate sitemap XML for search engines"""
    from flask import make_response, request

    # Get the base URL from the request
    base_url = request.url_root.rstrip('/')

    # Fetch dynamic content
    blogs = []
    webstories = []

    try:
        blogs = Blog.query.filter_by(status=True).order_by(Blog.updated_at.desc()).all()
    except Exception as e:
        print(f"Error fetching blogs for sitemap: {e}")

    try:
        webstories = WebStory.query.filter_by(status=True).order_by(WebStory.updated_at.desc()).all()
    except Exception as e:
        print(f"Error fetching webstories for sitemap: {e}")

    # Render the sitemap template
    xml_content = render_template(
        'sitemap.xml',
        base_url=base_url,
        blogs=blogs,
        webstories=webstories
    )

    # Create response with proper content type
    response = make_response(xml_content)
    response.headers['Content-Type'] = 'application/xml'
    response.headers['Cache-Control'] = 'public, max-age=3600'  # Cache for 1 hour

    return response


@app.route('/robots.txt')
def robots():
    """Generate robots.txt dynamically using template"""
    from flask import make_response, request

    base_url = request.url_root.rstrip('/')

    # Render the robots template
    robots_content = render_template('robots.txt', base_url=base_url)

    response = make_response(robots_content)
    response.headers['Content-Type'] = 'text/plain'

    return response


# Execute application
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        create_super_admin()
        run_sync_command()

    # Get host and port from environment variables
    host = os.getenv('FLASK_HOST', '127.0.0.1')
    port = int(os.getenv('FLASK_PORT', '5000'))
    debug = os.getenv('FLASK_DEBUG', 'True').lower() == 'true'

    print(f"\n{'='*70}")
    print(f"🚀 Starting Flask application on http://{host}:{port}")
    print(f"{'='*70}\n")

    app.run(host=host, port=port, debug=debug)