#!/usr/bin/env python3
"""Alert system integration for crash prevention.

This module integrates with various alerting channels to notify
about predicted crashes and preventive actions taken.
"""

import json
import os
import smtplib
import subprocess
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional

# Configuration
ALERT_CONFIG = {
    "email": {
        "enabled": False,
        "smtp_host": "",
        "smtp_port": 587,
        "username": "",
        "password": "",
        "recipients": []
    },
    "signal": {
        "enabled": False,
        "cli_path": "signal-cli"
    },
    "webhook": {
        "enabled": False,
        "url": "",
        "headers": {}
    },
    "log_file": "~/.hermes/logs/crash_alerts.log"
}

class AlertManager:
    """Manages alerts for crash prevention system."""
    
    def __init__(self, config: Optional[Dict] = None):
        self.config = config or ALERT_CONFIG
        self.log_file = Path(self.config["log_file"]).expanduser()
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
    
    def send_alert(self, title: str, message: str, severity: str = "INFO", 
                  channels: Optional[List[str]] = None) -> Dict:
        """Send alert through multiple channels."""
        if channels is None:
            channels = ["log"]  # Always log
        
        results = {}
        
        for channel in channels:
            try:
                if channel == "email":
                    results["email"] = self._send_email_alert(title, message, severity)
                elif channel == "signal":
                    results["signal"] = self._send_signal_alert(title, message, severity)
                elif channel == "webhook":
                    results["webhook"] = self._send_webhook_alert(title, message, severity)
                elif channel == "log":
                    results["log"] = self._log_alert(title, message, severity)
                else:
                    results[channel] = {"success": False, "error": f"Unknown channel: {channel}"}
                    
            except Exception as e:
                results[channel] = {"success": False, "error": str(e)}
        
        return results
    
    def _send_email_alert(self, title: str, message: str, severity: str) -> Dict:
        """Send alert via email."""
        if not self.config["email"]["enabled"]:
            return {"success": False, "error": "Email alerts disabled"}
        
        try:
            msg = MIMEText(f"""
CRASH PREVENTION ALERT
======================
Severity: {severity}
Title: {title}
Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}

{message}
""")
            msg['Subject'] = f"[CRASH PREVENTION {severity.upper()}] {title}"
            msg['From'] = self.config["email"]["username"]
            msg['To'] = ', '.join(self.config["email"]["recipients"])
            
            with smtplib.SMTP(
                self.config["email"]["smtp_host"],
                self.config["email"]["smtp_port"]
            ) as server:
                server.starttls()
                server.login(
                    self.config["email"]["username"],
                    self.config["email"]["password"]
                )
                server.send_message(msg)
            
            return {"success": True, "recipients": len(self.config["email"]["recipients"])}
            
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _send_signal_alert(self, title: str, message: str, severity: str) -> Dict:
        """Send alert via Signal."""
        if not self.config["signal"]["enabled"]:
            return {"success": False, "error": "Signal alerts disabled"}
        
        try:
            # Format message for Signal
            signal_msg = f"🚨 {severity.upper()}: {title}\n\n{message}"
            
            # Send to all recipients
            results = []
            for recipient in self.config["signal"].get("recipients", []):
                try:
                    result = subprocess.run([
                        self.config["signal"]["cli_path"],
                        "--config", self.config["signal"].get("config_path", "~/.config/signal"),
                        "send", "-m", signal_msg, recipient
                    ], capture_output=True, text=True, timeout=30)
                    
                    results.append({
                        "recipient": recipient,
                        "success": result.returncode == 0,
                        "output": result.stdout,
                        "error": result.stderr
                    })
                except Exception as e:
                    results.append({
                        "recipient": recipient,
                        "success": False,
                        "error": str(e)
                    })
            
            success_count = sum(1 for r in results if r["success"])
            return {
                "success": success_count > 0,
                "sent": success_count,
                "total": len(results),
                "details": results
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _send_webhook_alert(self, title: str, message: str, severity: str) -> Dict:
        """Send alert via webhook."""
        if not self.config["webhook"]["enabled"]:
            return {"success": False, "error": "Webhook alerts disabled"}
        
        try:
            import urllib.request
            
            payload = {
                "title": title,
                "message": message,
                "severity": severity,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "hermes-crash-prevention"
            }
            
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                self.config["webhook"]["url"],
                data=data,
                headers={
                    'Content-Type': 'application/json',
                    **self.config["webhook"]["headers"]
                }
            )
            
            with urllib.request.urlopen(req, timeout=30) as response:
                response_data = response.read().decode('utf-8')
            
            return {
                "success": response.status < 400,
                "status_code": response.status,
                "response": response_data
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _log_alert(self, title: str, message: str, severity: str) -> Dict:
        """Log alert to file."""
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        log_entry = f"[{timestamp}] [{severity}] {title}\n{message}\n{'='*60}\n"
        
        try:
            with open(self.log_file, 'a') as f:
                f.write(log_entry)
            
            return {"success": True, "log_file": str(self.log_file)}
        except Exception as e:
            return {"success": False, "error": str(e)}

def send_crash_warning_alert(resource_type: str, risk_level: str, 
                             minutes_to_crash: Optional[int], 
                             confidence: float, alert_manager: AlertManager) -> Dict:
    """Send crash warning alert."""
    title = f"Crash Risk: {resource_type} ({risk_level.upper()})"
    
    if minutes_to_crash:
        message = (
            f"Predicted crash for {resource_type} in {minutes_to_crash} minutes\n"
            f"Risk Level: {risk_level}\n"
            f"Confidence: {confidence:.1%}\n"
            f"Immediate action required!"
        )
        severity = "CRITICAL"
        channels = ["log", "email"]  # High priority
    else:
        message = (
            f"High resource pressure detected for {resource_type}\n"
            f"Risk Level: {risk_level}\n"
            f"Confidence: {confidence:.1%}\n"
            f"Monitor closely and consider preventive actions."
        )
        severity = "WARNING"
        channels = ["log"]
    
    return alert_manager.send_alert(title, message, severity, channels)

def send_preventive_action_alert(action: Dict, result: Dict, 
                                alert_manager: AlertManager) -> Dict:
    """Send alert for preventive action taken."""
    title = f"Preventive Action: {action['action']}"
    
    message = (
        f"Action: {action['action']}\n"
        f"Resource: {action['resource']}\n"
        f"Priority: {action['priority']}\n"
        f"Description: {action['description']}\n"
        f"Result: {'SUCCESS' if result.get('success') else 'FAILED'}\n"
    )
    
    if not result.get('success'):
        message += f"Error: {result.get('error', 'Unknown error')}"
        severity = "ERROR"
    else:
        message += f"Details: {json.dumps(result.get('result', {}), indent=2)}"
        severity = "INFO"
    
    channels = ["log"]
    if action['priority'] in ["high", "critical"]:
        channels.append("email")
    
    return alert_manager.send_alert(title, message, severity, channels)

# Global alert manager instance
_alert_manager = None

def get_alert_manager():
    """Get or create the global alert manager."""
    global _alert_manager
    if _alert_manager is None:
        # Load config if exists
        config_path = Path("~/.hermes/config/crash_alert_config.json").expanduser()
        if config_path.exists():
            with open(config_path) as f:
                config = json.load(f)
        else:
            config = ALERT_CONFIG
        
        _alert_manager = AlertManager(config)
    return _alert_manager

if __name__ == "__main__":
    # Test alert system
    alert_manager = get_alert_manager()
    
    # Send test alert
    result = alert_manager.send_alert(
        "Test Alert",
        "This is a test alert from the crash prevention system.",
        "INFO",
        ["log"]
    )
    
    print("Test alert result:")
    print(json.dumps(result, indent=2))