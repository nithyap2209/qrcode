"""
Migration script to add expiry notification tracking columns to subscribed_users table.
Run this script to add the new columns to your database.

Usage:
    python add_expiry_notification_columns.py
"""

from app import create_app
from models.database import db
from sqlalchemy import text

def add_expiry_notification_columns():
    """Add expiry notification tracking columns to subscribed_users table"""
    app = create_app()

    with app.app_context():
        try:
            # Check if columns already exist
            result = db.session.execute(text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'subscribed_users'
                AND column_name IN ('expiry_notification_sent', 'expiry_notification_sent_at')
            """))
            existing_columns = [row[0] for row in result.fetchall()]

            # Add expiry_notification_sent column if it doesn't exist
            if 'expiry_notification_sent' not in existing_columns:
                print("Adding expiry_notification_sent column...")
                db.session.execute(text("""
                    ALTER TABLE subscribed_users
                    ADD COLUMN expiry_notification_sent BOOLEAN DEFAULT FALSE
                """))
                print("✅ expiry_notification_sent column added")
            else:
                print("ℹ️ expiry_notification_sent column already exists")

            # Add expiry_notification_sent_at column if it doesn't exist
            if 'expiry_notification_sent_at' not in existing_columns:
                print("Adding expiry_notification_sent_at column...")
                db.session.execute(text("""
                    ALTER TABLE subscribed_users
                    ADD COLUMN expiry_notification_sent_at DATETIME NULL
                """))
                print("✅ expiry_notification_sent_at column added")
            else:
                print("ℹ️ expiry_notification_sent_at column already exists")

            db.session.commit()
            print("\n✅ Migration completed successfully!")

        except Exception as e:
            db.session.rollback()
            print(f"\n❌ Migration failed: {str(e)}")

            # Try MySQL syntax if PostgreSQL syntax fails
            try:
                print("\nTrying MySQL syntax...")
                db.session.execute(text("""
                    ALTER TABLE subscribed_users
                    ADD COLUMN expiry_notification_sent TINYINT(1) DEFAULT 0
                """))
                db.session.execute(text("""
                    ALTER TABLE subscribed_users
                    ADD COLUMN expiry_notification_sent_at DATETIME NULL
                """))
                db.session.commit()
                print("✅ Migration completed successfully with MySQL syntax!")
            except Exception as e2:
                db.session.rollback()
                print(f"❌ MySQL syntax also failed: {str(e2)}")
                print("\nPlease run the SQL migration manually:")
                print("  ALTER TABLE subscribed_users ADD COLUMN expiry_notification_sent BOOLEAN DEFAULT FALSE;")
                print("  ALTER TABLE subscribed_users ADD COLUMN expiry_notification_sent_at DATETIME NULL;")

if __name__ == '__main__':
    add_expiry_notification_columns()
