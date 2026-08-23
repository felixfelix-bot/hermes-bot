#!/bin/bash
cat > /home/c03rad0r/.config/systemd/user/zai-proxy.service.d/spend-cap.conf << 'EOF'
[Service]
Environment=SPEND_CAP_MANAGER=10
Environment=SPEND_CAP_WORKER=3
EOF
systemctl --user daemon-reload
systemctl --user restart zai-proxy
echo "Spend cap lowered to \$10 manager / \$3 worker at $(date)" >> /tmp/spend-cap-change.log
