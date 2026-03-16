# QR Code Generator

A full-featured QR Code Generator web application built with Flask. Create, customize, manage, and track QR codes with analytics.

## Features

- **Multiple QR Code Types** — URL, Text, vCard, Email, Phone, SMS, WhatsApp, WiFi, Events, and Image QR codes
- **Customization** — Custom colors, logos, patterns (rounded, circle, bars, gapped), and templates
- **Scan Analytics** — Track scans with location, device, and time data
- **User Authentication** — Registration, login, password reset with email verification
- **Subscription Plans** — Razorpay payment integration with tiered access
- **Admin Panel** — Manage users, subscriptions, blogs, web stories, and website settings
- **Email Notifications** — Subscription expiry reminders and contact form handling
- **Responsive Design** — Tailwind CSS for a modern, mobile-friendly UI

## Tech Stack

- **Backend:** Python, Flask, SQLAlchemy, Flask-Login, Flask-Mail
- **Frontend:** HTML, Tailwind CSS, JavaScript
- **Database:** SQLite (dev) / PostgreSQL (prod)
- **Payments:** Razorpay
- **QR Generation:** `qrcode` library with styled PIL images

## Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/nithyap2209/qrcode.git
   cd qrcode
   ```

2. **Create a virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**
   Create a `.env` file with:
   ```
   SECRET_KEY=your_secret_key
   SQLALCHEMY_DATABASE_URI=sqlite:///qr_codes.db
   MAIL_USERNAME=your_email
   MAIL_PASSWORD=your_app_password
   RAZORPAY_KEY_ID=your_razorpay_key
   RAZORPAY_KEY_SECRET=your_razorpay_secret
   ```

5. **Initialize the database**
   ```bash
   python init_database.py
   ```

6. **Run the application**
   ```bash
   python app.py
   ```

## License

This project is for educational and personal use.