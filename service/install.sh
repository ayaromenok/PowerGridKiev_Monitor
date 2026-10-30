#!/bin/sh
sudo cp ./pgkiev_monitor_py.service /etc/systemd/system/
sudo systemctl start pgkiev_monitor_py
sudo systemctl status pgkiev_monitor_py
sudo systemctl enable pgkiev_monitor_py