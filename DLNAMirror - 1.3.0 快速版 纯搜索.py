import os
import sys
import socket
import logging
import threading
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                            QHBoxLayout, QPushButton, QLabel, QTreeWidget,
                            QTreeWidgetItem, QHeaderView, QMessageBox)
from PyQt5.QtCore import Qt, QObject, pyqtSignal

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

class DLNADevice:
    def __init__(self, name, ip, location):
        self.name = name
        self.ip = ip
        self.location = location
        self.type = self._detect_type()
    
    def _detect_type(self):
        """简易设备类型检测"""
        if 'MediaRenderer' in self.location:
            return '渲染器'
        elif 'MediaServer' in self.location:
            return '服务器'
        return '设备'

class DiscoverWorker(QObject):
    found = pyqtSignal(list)
    finished = pyqtSignal()
    
    def run(self):
        devices = []
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(3)
        
        # 发送发现请求
        msg = '\r\n'.join([
            'M-SEARCH * HTTP/1.1',
            'HOST:239.255.255.250:1900',
            'MAN:"ssdp:discover"',
            'MX:3',
            'ST:ssdp:all',
            '',''])
        
        try:
            sock.sendto(msg.encode(), ('239.255.255.250', 1900))
            
            while True:
                try:
                    data, addr = sock.recvfrom(4096)
                    device = self._parse_response(data)
                    if device:
                        devices.append(device)
                except socket.timeout:
                    break
                except:
                    continue
            
            self.found.emit(devices)
        finally:
            sock.close()
            self.finished.emit()
    
    def _parse_response(self, data):
        """宽松的响应解析"""
        try:
            headers = {}
            for line in data.decode().split('\r\n')[1:]:
                if ':' in line:
                    key, val = line.split(':', 1)
                    headers[key.strip()] = val.strip()
            
            location = headers.get('Location', headers.get('LOCATION', ''))
            if not location.startswith('http'):
                return None
                
            ip = urlparse(location).hostname
            name = headers.get('USN', ip).split('::')[0]
            return DLNADevice(name, ip, location)
        except:
            return None

class DLNAMirrorApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DLNA投屏工具(快速版)")
        self.setMinimumSize(600, 400)
        
        # UI组件
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['名称', 'IP地址', '类型'])
        self.tree.setColumnWidth(0, 200)
        
        self.refresh_btn = QPushButton('刷新设备')
        self.refresh_btn.clicked.connect(self.start_discovery)
        
        # 布局
        layout = QVBoxLayout()
        layout.addWidget(self.tree)
        layout.addWidget(self.refresh_btn)
        
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)
        
        # 发现线程
        self.discover_thread = None
    
    def start_discovery(self):
        """开始设备发现"""
        self.tree.clear()
        self.refresh_btn.setEnabled(False)
        
        self.discover_thread = threading.Thread(target=self._discovery_task, daemon=True)
        self.discover_thread.start()
    
    def _discovery_task(self):
        """设备发现任务"""
        worker = DiscoverWorker()
        worker.found.connect(self._on_devices_found)
        worker.finished.connect(lambda: self.refresh_btn.setEnabled(True))
        worker.run()
    
    def _on_devices_found(self, devices):
        """更新设备列表"""
        self.tree.clear()
        
        # 按类型分组
        type_map = {}
        for dev in devices:
            if dev.type not in type_map:
                type_map[dev.type] = []
            type_map[dev.type].append(dev)
        
        # 添加到树形视图
        for dev_type, dev_list in type_map.items():
            parent = QTreeWidgetItem([f"{dev_type} ({len(dev_list)})", "", ""])
            for dev in dev_list:
                child = QTreeWidgetItem([dev.name, dev.ip, dev.type])
                parent.addChild(child)
            self.tree.addTopLevelItem(parent)
            parent.setExpanded(True)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # 确保使用Windows上的最佳Socket配置
    if os.name == 'nt':
        from ctypes import windll
        windll.wininet.InternetSetOptionW(0, 42, 0, 0)  # 禁用socket缓存
    
    window = DLNAMirrorApp()
    window.show()
    app.exec_()