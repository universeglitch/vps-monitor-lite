Node Monitor Panel Backup
=========================

Contents:
1. panel.py
   Current panel source code.

2. node-panel.service
   Clean systemd service template without credentials.

IMPORTANT SECURITY NOTES:
- This archive does not contain auth.json.
- This archive does not contain secret.key.
- This archive does not contain Xray configuration.
- This archive does not contain Cloudflare Tunnel credentials.
- Existing login credentials and sessions cannot be restored from this archive.

INSTALLATION:
1. Create the directory:
   mkdir -p /opt/node-panel

2. Copy panel.py:
   cp panel.py /opt/node-panel/panel.py

3. Install the service template:
   cp node-panel.service /etc/systemd/system/node-panel.service

4. Ensure the authentication files are separately configured:
   /etc/node-panel/auth.json
   /etc/node-panel/secret.key

5. Reload and start:
   systemctl daemon-reload
   systemctl enable node-panel
   systemctl restart node-panel

6. Check service status:
   systemctl status node-panel

NOTE:
The service template assumes the panel runs as root and uses
/usr/bin/python3. Verify the paths and authentication files
before restoring on another machine.

#installation
use install.sh
