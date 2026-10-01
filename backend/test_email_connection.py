"""
CircularFlow AI — Live Email Connection Verification Script
Run from backend directory:
    python test_email_connection.py [optional_recipient_email]
"""
import sys
import os

# Ensure UTF-8 output encoding for Windows command line
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Add backend directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import get_settings
from app.services.email_service import _get_config, _is_configured, test_resend_connectivity, _send_via_smtp

def run_test():
    recipient = sys.argv[1] if len(sys.argv) > 1 else None
    config = _get_config()
    
    print("\n" + "=" * 65)
    print("  CIRCULARFLOW AI - EMAIL CONFIGURATION & CONNECTIVITY TEST")
    print("=" * 65)
    
    print(f"EMAIL_ENABLED           : {config['enabled']}")
    print(f"EMAIL_PROVIDER_API_KEY  : {'[Configured]' if config['api_key'] else '[NOT CONFIGURED]'}")
    print(f"EMAIL_FROM              : {config['from_addr']}")
    print(f"EMAIL_FROM_NAME         : {config['from_name']}")
    print(f"SMTP_HOST               : {config['smtp_host'] or '[NOT CONFIGURED]'}")
    print(f"SMTP_PORT               : {config['smtp_port']}")
    print(f"SMTP_USER               : {config['smtp_user'] or '[NOT CONFIGURED]'}")
    print(f"SMTP_TLS                : {config['smtp_tls']}")
    print("-" * 65)

    if not config["enabled"]:
        print("\n[ERROR] EMAIL_ENABLED is set to 'false'. Enable it in backend/.env to send emails.\n")
        return

    if not _is_configured(config):
        print("\n[!] No email delivery method configured in backend/.env!")
        print("\nTo fix this, choose one of two options:\n")
        print("  Option 1: Resend (Recommended)")
        print("    1. Sign up at https://resend.com and create an API key.")
        print("    2. In backend/.env set:")
        print("         EMAIL_PROVIDER_API_KEY=re_xxxxxxxxxxxxxxxx")
        print("         EMAIL_FROM=onboarding@resend.dev")
        print("         EMAIL_FROM_NAME=CircularFlow AI - Vignan's University")
        print("\n  Option 2: Gmail SMTP (or other SMTP)")
        print("    1. Generate an App Password in your Google Account Security settings.")
        print("    2. In backend/.env set:")
        print("         SMTP_HOST=smtp.gmail.com")
        print("         SMTP_PORT=587")
        print("         SMTP_USER=your_email@gmail.com")
        print("         SMTP_PASSWORD=your_16_digit_app_password")
        print("         SMTP_TLS=true")
        print("         EMAIL_FROM=your_email@gmail.com")
        print("=" * 65 + "\n")
        return

    # Option 1: Resend API
    if config["api_key"]:
        print("\n[1/1] Testing Resend API connection...")
        result = test_resend_connectivity()
        if result.get("success"):
            print(f"[SUCCESS] Resend accepted the test email!")
            print(f"   Email ID : {result.get('email_id', 'N/A')}")
            print(f"   Message  : {result.get('message')}")
        else:
            print(f"[FAILED] Resend delivery failed.")
            print(f"   Status Code : {result.get('status_code')}")
            print(f"   Error       : {result.get('error')}")
            if result.get("details"):
                print(f"   Details     : {result.get('details')}")
        print("=" * 65 + "\n")
        return

    # Option 2: SMTP
    if config["smtp_host"]:
        target_to = recipient or config["smtp_user"]
        if not target_to:
            print("[FAILED] Cannot test SMTP: No recipient email provided. Pass recipient email: python test_email_connection.py your_email@domain.com")
            return
            
        print(f"\n[1/1] Testing SMTP connection to {config['smtp_host']}:{config['smtp_port']}...")
        try:
            _send_via_smtp(
                smtp_host=config["smtp_host"],
                smtp_port=config["smtp_port"],
                smtp_user=config["smtp_user"],
                smtp_password=config["smtp_password"],
                smtp_tls=config["smtp_tls"],
                from_addr=config["from_addr"] or config["smtp_user"],
                from_name=config["from_name"],
                to_email=target_to,
                to_name="Test Recipient",
                subject="CircularFlow AI - SMTP Connectivity Test",
                html="<h3>CircularFlow AI</h3><p>SMTP connectivity test successful!</p>",
                plain="CircularFlow AI: SMTP connectivity test successful!",
            )
            print(f"[SUCCESS] SMTP email successfully delivered to {target_to}.")
        except Exception as exc:
            print(f"[FAILED] SMTP delivery failed: {exc}")
        print("=" * 65 + "\n")

if __name__ == "__main__":
    run_test()
