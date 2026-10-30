#!/bin/sh
sudo systemctl stop pgkiev_monitor_py
sudo systemctl status pgkiev_monitor_py
sudo systemctl disable pgkiev_monitor_py
sudo rm /etc/systemd/system/pgkiev_monitor_py.service
