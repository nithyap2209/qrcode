-- Migration: Add expiry notification tracking columns to subscribed_users table
-- Run this SQL on your database to add the new columns

-- Add expiry_notification_sent column (boolean, default false)
ALTER TABLE subscribed_users
ADD COLUMN IF NOT EXISTS expiry_notification_sent BOOLEAN DEFAULT FALSE;

-- Add expiry_notification_sent_at column (datetime, nullable)
ALTER TABLE subscribed_users
ADD COLUMN IF NOT EXISTS expiry_notification_sent_at DATETIME NULL;

-- For MySQL, use this instead:
-- ALTER TABLE subscribed_users ADD COLUMN expiry_notification_sent TINYINT(1) DEFAULT 0;
-- ALTER TABLE subscribed_users ADD COLUMN expiry_notification_sent_at DATETIME NULL;

-- Verify the columns were added
-- SELECT column_name, data_type, is_nullable, column_default
-- FROM information_schema.columns
-- WHERE table_name = 'subscribed_users'
-- AND column_name IN ('expiry_notification_sent', 'expiry_notification_sent_at');
