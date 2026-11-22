import os
import sys
import logging
import logging.handlers
import threading
import time
import queue
import socket
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
from datetime import datetime
from enum import Enum, auto
from typing import Optional, Dict, List, Tuple, Any
import requests
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                            QHBoxLayout, QPushButton, QLabel, QComboBox, 
                            QMessageBox, QGroupBox, QPlainTextEdit, QTreeWidget,
                            QTreeWidgetItem, QHeaderView, QLineEdit)
from PyQt5.QtCore import Qt, QSize, QObject, pyqtSignal, QTimer, QUrl
from PyQt5.QtGui import QIcon, QDesktopServices

# 配置日志系统
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('dlna_mirror.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class MirrorStatus(Enum):
    """投屏状态枚举"""
    IDLE = auto()
    DISCOVERING = auto()
    CONNECTING = auto()
    MIRRORING = auto()
    STOPPING = auto()
    ERROR = auto()

class UPNPDiscoverer:
    """增强版UPnP设备发现类"""
    @staticmethod
    def discover(timeout=3) -> List[Dict[str, Any]]:
        """使用ssdp:all发现所有UPnP设备"""
        devices = []
        seen_udns = set()
        
        # 创建UDP套接字
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(timeout)
        
        try:
            # 发送ssdp:all搜索请求
            msg = (
                'M-SEARCH * HTTP/1.1\r\n'
                'HOST: 239.255.255.250:1900\r\n'
                'MAN: "ssdp:discover"\r\n'
                'MX: 3\r\n'
                'ST: ssdp:all\r\n'  # 搜索所有设备
                '\r\n'
            ).encode('utf-8')
            sock.sendto(msg, ('239.255.255.250', 1900))
            
            # 接收响应
            start_time = time.time()
            while time.time() - start_time < timeout:
                try:
                    data, addr = sock.recvfrom(4096)
                    device = UPNPDiscoverer._parse_response(data, addr)
                    if device and device['udn'] not in seen_udns:
                        seen_udns.add(device['udn'])
                        devices.append(device)
                except socket.timeout:
                    break
                except Exception as e:
                    logger.warning(f"解析设备响应错误: {str(e)}")
                    continue
        finally:
            sock.close()
        
        return devices
    
    @staticmethod
    def _parse_response(data: bytes, addr: Tuple[str, int]) -> Optional[Dict[str, Any]]:
        """解析SSDP响应"""
        try:
            headers = {}
            lines = data.decode('utf-8').split('\r\n')
            
            # 解析头部
            for line in lines[1:]:  # 跳过状态行
                if ':' in line:
                    key, value = line.split(':', 1)
                    headers[key.strip().upper()] = value.strip()
            
            # 必需字段检查
            if not all(k in headers for k in ['LOCATION', 'USN', 'SERVER', 'ST']):
                return None
            
            # 从USN提取UDN (唯一设备标识)
            usn = headers['USN']
            udn = usn.split('::')[0] if '::' in usn else usn
            
            # 解析LOCATION URL
            location = headers['LOCATION']
            parsed = urlparse(location)
            
            return {
                'name': udn.split(':')[-1] if ':' in udn else udn,
                'ip': parsed.hostname,
                'port': parsed.port,
                'location': location,
                'type': headers['ST'],
                'server': headers['SERVER'],
                'udn': udn
            }
        except Exception as e:
            logger.warning(f"解析响应失败: {str(e)}")
            return None

class DLNADevice:
    """DLNA设备类"""
    def __init__(self, device_info: Dict[str, Any]):
        self.udn = device_info['udn']
        self.name = device_info['name']
        self.ip = device_info['ip']
        self.port = device_info['port']
        self.location = device_info['location']
        self.device_type = self._parse_device_type(device_info['type'])
        self.server_info = device_info['server']
        
        # 从设备描述文档获取的详细信息
        self.manufacturer = ""
        self.model_name = ""
        self.model_number = ""
        self.serial_number = ""
        self.icon_url = ""
        self.services = []
        
        # 解析设备描述文档
        self._parse_device_description()
    
    def _parse_device_type(self, st: str) -> str:
        """解析设备类型"""
        if "MediaRenderer" in st:
            return "MediaRenderer"
        elif "MediaServer" in st:
            return "MediaServer"
        return st.split(':')[-1] if ':' in st else st
    
    def _parse_device_description(self):
        """解析设备描述文档"""
        try:
            response = requests.get(self.location, timeout=3)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            
            device = root.find(".//{urn:schemas-upnp-org:device-1-0}device")
            if device is not None:
                self.manufacturer = device.findtext("{urn:schemas-upnp-org:device-1-0}manufacturer", "").strip()
                self.model_name = device.findtext("{urn:schemas-upnp-org:device-1-0}modelName", "").strip()
                self.model_number = device.findtext("{urn:schemas-upnp-org:device-1-0}modelNumber", "").strip()
                self.serial_number = device.findtext("{urn:schemas-upnp-org:device-1-0}serialNumber", "").strip()
                
                # 获取图标
                icon = device.find(".//{urn:schemas-upnp-org:device-1-0}icon")
                if icon is not None:
                    base_url = f"http://{self.ip}:{self.port}"
                    icon_url = icon.findtext("{urn:schemas-upnp-org:device-1-0}url", "").strip()
                    self.icon_url = base_url + icon_url if icon_url.startswith("/") else icon_url
            
            # 解析服务
            service_list = root.find(".//{urn:schemas-upnp-org:device-1-0}serviceList")
            if service_list is not None:
                for service in service_list:
                    service_type = service.findtext("{urn:schemas-upnp-org:device-1-0}serviceType", "").strip()
                    control_url = service.findtext("{urn:schemas-upnp-org:device-1-0}controlURL", "").strip()
                    self.services.append({
                        "type": service_type,
                        "control_url": control_url
                    })
        except Exception as e:
            logger.warning(f"解析设备描述失败: {str(e)}")
    
    def get_display_info(self) -> Dict[str, str]:
        """获取显示信息"""
        return {
            "名称": self.name,
            "制造商": self.manufacturer,
            "型号": f"{self.model_name} {self.model_number}",
            "序列号": self.serial_number,
            "类型": self._get_friendly_type(),
            "IP地址": f"{self.ip}:{self.port}",
            "服务URL": self.location,
            "唯一标识": self.udn,
            "服务端信息": self.server_info
        }
    
    def _get_friendly_type(self) -> str:
        """获取友好的设备类型名称"""
        type_map = {
            "MediaRenderer": "媒体渲染器",
            "MediaServer": "媒体服务器",
            "ContentDirectory": "内容目录",
            "ConnectionManager": "连接管理器",
            "AVTransport": "AV传输服务"
        }
        return type_map.get(self.device_type, self.device_type)
    
    def __str__(self):
        return f"{self.name} ({self.manufacturer} {self.model_name})"

class DLNAMirrorApp(QMainWindow):
    """主应用程序窗口"""
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DLNA投屏工具 (SSDP版)")
        self.setWindowIcon(QIcon("dlna_mirror.png"))
        self.setMinimumSize(QSize(800, 600))
        
        self.current_status = MirrorStatus.IDLE
        self.current_worker = None
        self.discovered_devices = []
        self.log_queue = queue.Queue()
        
        self.init_ui()
        
        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self.update_log_display)
        self.log_timer.start(500)
        
        self._setup_logging()
        self.refresh_devices()
    
    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout()
        main_widget.setLayout(main_layout)
        
        # 设备发现组
        discover_group = QGroupBox("设备发现")
        discover_layout = QVBoxLayout()
        
        self.device_tree = QTreeWidget()
        self.device_tree.setHeaderLabels(["设备名称", "类型", "IP地址"])
        self.device_tree.setColumnCount(3)
        self.device_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.device_tree.itemDoubleClicked.connect(self.show_device_details)
        
        # 控制按钮
        btn_layout = QHBoxLayout()
        self.refresh_btn = QPushButton("刷新设备")
        self.refresh_btn.clicked.connect(self.refresh_devices)
        
        self.info_btn = QPushButton("设备详情")
        self.info_btn.clicked.connect(self.show_device_details)
        self.info_btn.setEnabled(False)
        
        self.start_btn = QPushButton("开始投屏")
        self.start_btn.clicked.connect(self.start_mirroring)
        self.start_btn.setEnabled(False)
        
        self.stop_btn = QPushButton("停止投屏")
        self.stop_btn.clicked.connect(self.stop_mirroring)
        self.stop_btn.setEnabled(False)
        
        btn_layout.addWidget(self.refresh_btn)
        btn_layout.addWidget(self.info_btn)
        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.stop_btn)
        
        # 媒体URL
        media_layout = QHBoxLayout()
        media_layout.addWidget(QLabel("媒体URL:"))
        self.media_url = QLineEdit("http://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4")
        media_layout.addWidget(self.media_url)
        
        discover_layout.addWidget(self.device_tree)
        discover_layout.addLayout(btn_layout)
        discover_layout.addLayout(media_layout)
        discover_group.setLayout(discover_layout)
        
        # 状态和日志
        self.status_label = QLabel("状态: 等待开始")
        self.status_label.setAlignment(Qt.AlignCenter)
        
        self.log_display = QPlainTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setMaximumBlockCount(1000)
        
        main_layout.addWidget(discover_group)
        main_layout.addWidget(self.status_label)
        main_layout.addWidget(QLabel("运行日志:"))
        main_layout.addWidget(self.log_display)
    
    def _setup_logging(self):
        queue_handler = logging.handlers.QueueHandler(self.log_queue)
        queue_handler.setLevel(logging.INFO)
        logger.addHandler(queue_handler)
    
    def refresh_devices(self):
        if self.current_status != MirrorStatus.IDLE:
            QMessageBox.warning(self, "警告", "请先停止当前操作")
            return
        
        self._set_status(MirrorStatus.DISCOVERING)
        self.start_btn.setEnabled(False)
        self.info_btn.setEnabled(False)
        self.device_tree.clear()
        
        def discover_task():
            try:
                # 使用ssdp:all发现设备
                device_infos = UPNPDiscoverer.discover()
                
                # 创建设备对象
                devices = []
                for info in device_infos:
                    try:
                        device = DLNADevice(info)
                        devices.append(device)
                    except Exception as e:
                        logger.warning(f"创建设备失败: {str(e)}")
                
                self.discovered_devices = devices
                
                # 按设备类型分组
                type_groups = {}
                for device in devices:
                    if device.device_type not in type_groups:
                        type_groups[device.device_type] = []
                    type_groups[device.device_type].append(device)
                
                # 更新UI
                for dev_type, dev_list in type_groups.items():
                    type_item = QTreeWidgetItem([device._get_friendly_type(dev_type), "", ""])
                    type_item.setData(0, Qt.UserRole, None)
                    
                    for device in dev_list:
                        dev_item = QTreeWidgetItem([
                            str(device),
                            device._get_friendly_type(),
                            f"{device.ip}:{device.port}"
                        ])
                        dev_item.setData(0, Qt.UserRole, device)
                        type_item.addChild(dev_item)
                    
                    self.device_tree.addTopLevelItem(type_item)
                    type_item.setExpanded(True)
                
                if devices:
                    self._set_status(MirrorStatus.IDLE, f"找到 {len(devices)} 个设备")
                    self.start_btn.setEnabled(True)
                    self.info_btn.setEnabled(True)
                else:
                    self._set_status(MirrorStatus.IDLE, "未找到设备")
                
            except Exception as e:
                logger.error(f"设备发现失败: {str(e)}")
                self._set_status(MirrorStatus.ERROR, f"设备发现失败: {str(e)}")
        
        threading.Thread(target=discover_task, daemon=True).start()
    
    def get_selected_device(self) -> Optional[DLNADevice]:
        item = self.device_tree.currentItem()
        return item.data(0, Qt.UserRole) if item else None
    
    def show_device_details(self):
        device = self.get_selected_device()
        if device:
            dialog = DeviceInfoDialog(device, self)
            dialog.exec_()
    
    def start_mirroring(self):
        if self.current_status != MirrorStatus.IDLE:
            return
            
        device = self.get_selected_device()
        if not device:
            QMessageBox.warning(self, "警告", "请选择设备")
            return
            
        media_url = self.media_url.text().strip()
        if not media_url:
            QMessageBox.warning(self, "警告", "请输入媒体URL")
            return
            
        self._set_status(MirrorStatus.CONNECTING, f"连接 {device.name}...")
        
        # 这里应该添加实际的投屏逻辑
        # 暂时只显示状态变化
        QTimer.singleShot(2000, lambda: self._set_status(MirrorStatus.MIRRORING, f"正在投屏到 {device.name}"))
        
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.refresh_btn.setEnabled(False)
        self.info_btn.setEnabled(False)

    def stop_mirroring(self):
        if self.current_status not in [MirrorStatus.MIRRORING, MirrorStatus.CONNECTING]:
            return
            
        self._set_status(MirrorStatus.STOPPING, "正在停止...")
        self.stop_btn.setEnabled(False)
        
        # 这里应该添加实际的停止投屏逻辑
        # 暂时只显示状态变化
        QTimer.singleShot(1000, self._on_mirroring_finished)

    def _on_mirroring_finished(self):
        self._set_status(MirrorStatus.IDLE, "已停止")
        self.start_btn.setEnabled(len(self.discovered_devices) > 0)
        self.stop_btn.setEnabled(False)
        self.refresh_btn.setEnabled(True)
        self.info_btn.setEnabled(True)


    def _set_status(self, status: MirrorStatus, message: str = None):
        self.current_status = status
        if message:
            self.status_label.setText(f"状态: {message}")
    
    def update_log_display(self):
        while not self.log_queue.empty():
            self.log_display.appendPlainText(self.log_queue.get())
    
    def closeEvent(self, event):
        if self.current_status in [MirrorStatus.MIRRORING, MirrorStatus.CONNECTING]:
            reply = QMessageBox.question(
                self, "确认退出",
                "投屏正在进行中，确定要退出吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self.stop_mirroring()
                event.accept()
            else:
                event.ignore()
        else:
            self.log_timer.stop()
            event.accept()

class DeviceInfoDialog(QMessageBox):
    """设备信息对话框"""
    def __init__(self, device: DLNADevice, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设备详情")
        self.setIcon(QMessageBox.Information)
        
        info = device.get_display_info()
        text = "\n".join(f"{key}: {value}" for key, value in info.items())
        self.setText(text)
        
        if device.icon_url:
            self.addButton("查看图标", QMessageBox.ActionRole)
            self.buttonClicked.connect(lambda: QDesktopServices.openUrl(QUrl(device.icon_url)))
        
        self.addButton("关闭", QMessageBox.AcceptRole)

def check_dependencies():
    missing = []
    try:
        import PyQt5
        import requests
    except ImportError as e:
        missing.append(str(e).split()[-1])
    
    if missing:
        print("缺少依赖:", ", ".join(missing))
        print("请执行: pip install", " ".join(missing))
        return False
    return True

def main():
    if not check_dependencies():
        return 1
    
    app = QApplication(sys.argv)
    try:
        window = DLNAMirrorApp()
        window.show()
        return app.exec_()
    except Exception as e:
        logger.critical(f"启动失败: {str(e)}")
        QMessageBox.critical(None, "错误", f"程序启动失败:\n{str(e)}")
        return 1
    finally:
        logger.info("程序退出")

if __name__ == "__main__":
    sys.exit(main())