from .database import db
from datetime import datetime, UTC
import json

class QRCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    unique_id = db.Column(db.String(36), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    qr_type = db.Column(db.String(50), nullable=False)
    is_dynamic = db.Column(db.Boolean, default=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Basic styling
    color = db.Column(db.String(20), default='#000000')
    background_color = db.Column(db.String(20), default='#FFFFFF')
    logo_path = db.Column(db.String(200), nullable=True)
    frame_type = db.Column(db.String(50), nullable=True)
    shape = db.Column(db.String(50), default='square')
    
    # Advanced styling options
    template = db.Column(db.String(50), nullable=True)
    custom_eyes = db.Column(db.Boolean, default=False)
    inner_eye_style = db.Column(db.String(50), nullable=True)
    outer_eye_style = db.Column(db.String(50), nullable=True)
    inner_eye_color = db.Column(db.String(20), nullable=True)
    outer_eye_color = db.Column(db.String(20), nullable=True)
    module_size = db.Column(db.Integer, default=10)
    quiet_zone = db.Column(db.Integer, default=4)
    error_correction = db.Column(db.String(1), default='H')
    
    # NEW GRADIENT COLUMN - Boolean to explicitly track gradient usage
    gradient = db.Column(db.Boolean, nullable=False, default=False)
    
    gradient_start = db.Column(db.String(20), nullable=True)
    gradient_end = db.Column(db.String(20), nullable=True)
    export_type = db.Column(db.String(20), default='png')
    watermark_text = db.Column(db.String(100), nullable=True)
    logo_size_percentage = db.Column(db.Integer, default=25)
    round_logo = db.Column(db.Boolean, default=False)
    frame_text = db.Column(db.String(100), nullable=True)
    
    # New gradient options
    gradient_type = db.Column(db.String(20), nullable=True)
    gradient_direction = db.Column(db.String(20), nullable=True)
    
    # Frame color
    frame_color = db.Column(db.String(20), nullable=True)
    
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    scans = db.relationship('Scan', backref='qr_code', lazy=True)
# Add these models after your existing QRCode class

class QREmail(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    qr_code_id = db.Column(db.Integer, db.ForeignKey('qr_code.id'), nullable=False, unique=True)
    email = db.Column(db.String(255), nullable=False)
    subject = db.Column(db.String(255), nullable=True)
    body = db.Column(db.Text, nullable=True)
    
    # Relationship to parent QR code
    qr_code = db.relationship('QRCode', backref=db.backref('email_detail', uselist=False))

class QRPhone(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    qr_code_id = db.Column(db.Integer, db.ForeignKey('qr_code.id'), nullable=False, unique=True)
    phone = db.Column(db.String(50), nullable=False)
    
    # Relationship to parent QR code
    qr_code = db.relationship('QRCode', backref=db.backref('phone_detail', uselist=False))

class QRSms(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    qr_code_id = db.Column(db.Integer, db.ForeignKey('qr_code.id'), nullable=False, unique=True)
    phone = db.Column(db.String(50), nullable=False)
    message = db.Column(db.Text, nullable=True)
    
    # Relationship to parent QR code
    qr_code = db.relationship('QRCode', backref=db.backref('sms_detail', uselist=False))

class QRWhatsApp(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    qr_code_id = db.Column(db.Integer, db.ForeignKey('qr_code.id'), nullable=False, unique=True)
    phone = db.Column(db.String(50), nullable=False)
    message = db.Column(db.Text, nullable=True)
    
    # Relationship to parent QR code
    qr_code = db.relationship('QRCode', backref=db.backref('whatsapp_detail', uselist=False))

# In your models section, update the QRVCard class:

class QRVCard(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    qr_code_id = db.Column(db.Integer, db.ForeignKey('qr_code.id'), nullable=False, unique=True)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(50), nullable=True)
    email = db.Column(db.String(255), nullable=True)
    company = db.Column(db.String(100), nullable=True)
    title = db.Column(db.String(100), nullable=True)
    address = db.Column(db.Text, nullable=True)
    website = db.Column(db.String(255), nullable=True)
    
    # New fields for enhanced vCard
    logo_path = db.Column(db.String(200), nullable=True)
    primary_color = db.Column(db.String(20), nullable=True, default='#3366CC')
    secondary_color = db.Column(db.String(20), nullable=True, default='#5588EE')
    social_media = db.Column(db.Text, nullable=True)  # JSON storing social media links
    
    # Relationship to parent QR code
    qr_code = db.relationship('QRCode', backref=db.backref('vcard_detail', uselist=False))

class QREvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    qr_code_id = db.Column(db.Integer, db.ForeignKey('qr_code.id'), nullable=False, unique=True)
    title = db.Column(db.String(255), nullable=False)
    location = db.Column(db.String(255), nullable=True)
    start_date = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=True)
    description = db.Column(db.Text, nullable=True)
    organizer = db.Column(db.String(100), nullable=True)
    
    # Relationship to parent QR code
    qr_code = db.relationship('QRCode', backref=db.backref('event_detail', uselist=False))

class QRWifi(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    qr_code_id = db.Column(db.Integer, db.ForeignKey('qr_code.id'), nullable=False, unique=True)
    ssid = db.Column(db.String(100), nullable=False)
    password = db.Column(db.String(100), nullable=True)
    encryption = db.Column(db.String(20), nullable=True, default='WPA')
    
    # Relationship to parent QR code
    qr_code = db.relationship('QRCode', backref=db.backref('wifi_detail', uselist=False))

class QRText(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    qr_code_id = db.Column(db.Integer, db.ForeignKey('qr_code.id'), nullable=False, unique=True)
    text = db.Column(db.Text, nullable=False)

    # Relationship to parent QR code
    qr_code = db.relationship('QRCode', backref=db.backref('text_detail', uselist=False))

class QRImage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    qr_code_id = db.Column(db.Integer, db.ForeignKey('qr_code.id'), nullable=False, unique=True)
    image_path = db.Column(db.String(500), nullable=False)
    caption = db.Column(db.String(255), nullable=True)
    bg_color_1 = db.Column(db.String(20), nullable=True, default='#0a0a0a')
    bg_color_2 = db.Column(db.String(20), nullable=True, default='#1a1a2e')
    bg_direction = db.Column(db.String(30), nullable=True, default='to bottom')

    # Relationship to parent QR code
    qr_code = db.relationship('QRCode', backref=db.backref('image_detail', uselist=False))

class QRLink(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    qr_code_id = db.Column(db.Integer, db.ForeignKey('qr_code.id'), nullable=False, unique=True)
    url = db.Column(db.String(2000), nullable=False)
    
    # Relationship to parent QR code
    qr_code = db.relationship('QRCode', backref=db.backref('link_detail', uselist=False))

class Scan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    qr_code_id = db.Column(db.Integer, db.ForeignKey('qr_code.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    ip_address = db.Column(db.String(50), nullable=True)
    user_agent = db.Column(db.String(200), nullable=True)
    location = db.Column(db.String(100), nullable=True)
    os = db.Column(db.String(50), nullable=True)

# Add a new model for QR templates
class QRTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    qr_type = db.Column(db.String(50), nullable=False)
    html_content = db.Column(db.Text, nullable=False)
    css_content = db.Column(db.Text, nullable=True)
    js_content = db.Column(db.Text, nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f"<QRTemplate {self.name}>"