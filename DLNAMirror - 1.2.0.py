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

class ResourceManager:
    """资源管理类"""
    def __init__(self):
        self._resources = []
    
    def add(self, resource, cleanup_func):
        self._resources.append((resource, cleanup_func))
        return resource
    
    def cleanup(self):
        errors = []
        for resource, cleanup_func in reversed(self._resources):
            try:
                if resource is not None:
                    cleanup_func(resource)
            except Exception as e:
                errors.append(f"清理 {type(resource)} 失败: {str(e)}")
        if errors:
            logger.error("资源清理错误:\n" + "\n".join(errors))
        self._resources = []

class WorkerSignals(QObject):
    """工作线程信号"""
    status_changed = pyqtSignal(str)
    devices_updated = pyqtSignal(list)
    error_occurred = pyqtSignal(str)
    operation_completed = pyqtSignal()

class DLNADevice:
    """DLNA设备类"""
    def __init__(self, name: str, ip: str, port: int, service_url: str, device_type: str):
        self.name = name
        self.ip = ip
        self.port = port
        self.service_url = service_url
        self.device_type = device_type
        self.manufacturer = ""
        self.model_name = ""
        self.model_number = ""
        self.serial_number = ""
        self.udn = ""
        self.icon_url = ""
        self.services = []
        self._parse_device_info()
    
    def _parse_device_info(self):
        """解析设备描述文档"""
        try:
            response = requests.get(self.service_url, timeout=3)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            
            device = root.find(".//{urn:schemas-upnp-org:device-1-0}device")
            if device is not None:
                self.manufacturer = device.findtext("{urn:schemas-upnp-org:device-1-0}manufacturer", "").strip()
                self.model_name = device.findtext("{urn:schemas-upnp-org:device-1-0}modelName", "").strip()
                self.model_number = device.findtext("{urn:schemas-upnp-org:device-1-0}modelNumber", "").strip()
                self.serial_number = device.findtext("{urn:schemas-upnp-org:device-1-0}serialNumber", "").strip()
                self.udn = device.findtext("{urn:schemas-upnp-org:device-1-0}UDN", "").strip()
                
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
            logger.warning(f"解析设备信息失败: {str(e)}")

    def get_display_info(self) -> Dict[str, str]:
        """获取显示信息"""
        return {
            "名称": self.name,
            "制造商": self.manufacturer,
            "型号": f"{self.model_name} {self.model_number}",
            "序列号": self.serial_number,
            "类型": self._get_friendly_type(),
            "IP地址": f"{self.ip}:{self.port}",
            "服务URL": self.service_url,
            "唯一标识": self.udn
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

class UPNPDiscoverer:
    """UPnP设备发现类"""
    @staticmethod
    def discover(timeout=3) -> List[DLNADevice]:
        """发现设备"""
        devices = []
        seen_udns = set()
        
        # 发送多种搜索类型确保发现所有设备
        search_types = [
            "ssdp:all",
            "upnp:rootdevice",
            "urn:schemas-upnp-org:device:MediaRenderer:1",
            "urn:schemas-upnp-org:device:MediaServer:1"
        ]
        
        for st in search_types:
            devices.extend(UPNPDiscoverer._discover_with_st(st, timeout, seen_udns))
        
        return devices
    
    @staticmethod
    def _discover_with_st(st: str, timeout: int, seen_udns: set) -> List[DLNADevice]:
        """使用特定搜索类型发现设备"""
        devices = []
        
        msg = (
            'M-SEARCH * HTTP/1.1\r\n'
            'HOST: 239.255.255.250:1900\r\n'
            'MAN: "ssdp:discover"\r\n'
            'MX: 3\r\n'
            f'ST: {st}\r\n'
            '\r\n'
        ).encode('utf-8')
        
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        
        try:
            sock.sendto(msg, ('239.255.255.250', 1900))
            sock.settimeout(timeout)
            
            while True:
                try:
                    data, addr = sock.recvfrom(4096)
                    device = UPNPDiscoverer._parse_response(data, addr, st)
                    if device and device.udn not in seen_udns:
                        seen_udns.add(device.udn)
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
    def _parse_response(data: bytes, addr: Tuple[str, int], search_type: str) -> Optional[DLNADevice]:
        """解析响应"""
        try:
            headers = {}
            lines = data.decode('utf-8').split('\r\n')
            
            for line in lines[1:]:
                if ':' in line:
                    key, value = line.split(':', 1)
                    headers[key.strip().upper()] = value.strip()
            
            if 'LOCATION' not in headers or 'USN' not in headers:
                return None
                
            location = headers['LOCATION']
            usn = headers['USN']
            st = headers.get('ST', search_type)
            
            parsed = urlparse(location)
            name = usn.split('::')[0] if '::' in usn else parsed.hostname
            
            # 初步创建设备对象获取UDN
            temp_device = DLNADevice(
                name=name,
                ip=parsed.hostname,
                port=parsed.port,
                service_url=location,
                device_type=st
            )
            
            return temp_device
            
        except Exception as e:
            logger.warning(f"解析设备响应失败: {str(e)}")
            return None

class DLNAControlPoint:
    """DLNA控制点"""
    def __init__(self, device: DLNADevice):
        self.device = device
        self.av_transport_service = None
        self.rendering_control_service = None
        self._find_services()
    
    def _find_services(self):
        """查找服务"""
        for service in self.device.services:
            if "AVTransport" in service["type"]:
                self.av_transport_service = service
            elif "RenderingControl" in service["type"]:
                self.rendering_control_service = service
    
    def _send_soap_request(self, service, action, params):
        """发送SOAP请求"""
        if not service:
            raise ValueError("服务未找到")
            
        control_url = service["control_url"]
        if control_url.startswith("/"):
            base_url = f"http://{self.device.ip}:{self.device.port}"
            control_url = base_url + control_url
        
        headers = {
            'Content-Type': 'text/xml; charset=utf-8',
            'SOAPAction': f'"{service["type"]}#{action}"'
        }
        
        body = f'''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" 
s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:{action} xmlns:u="{service["type"]}">
'''
        for key, value in params.items():
            body += f'<{key}>{value}</{key}>'
        body += f'</u:{action}>\n</s:Body>\n</s:Envelope>'
        
        try:
            response = requests.post(control_url, data=body, headers=headers, timeout=5)
            response.raise_for_status()
            return ET.fromstring(response.content)
        except Exception as e:
            logger.error(f"SOAP请求失败: {str(e)}")
            raise
    
    def set_av_transport_uri(self, uri: str, meta_data: str = ""):
        """设置URI"""
        params = {
            'InstanceID': '0',
            'CurrentURI': uri,
            'CurrentURIMetaData': meta_data
        }
        return self._send_soap_request(self.av_transport_service, 'SetAVTransportURI', params)
    
    def play(self):
        """播放"""
        params = {
            'InstanceID': '0',
            'Speed': '1'
        }
        return self._send_soap_request(self.av_transport_service, 'Play', params)
    
    def stop(self):
        """停止"""
        params = {
            'InstanceID': '0'
        }
        return self._send_soap_request(self.av_transport_service, 'Stop', params)

class DLNAMirrorWorker(threading.Thread):
    """DLNA工作线程"""
    def __init__(self, device: DLNADevice, media_url: str):
        super().__init__(daemon=True)
        self.device = device
        self.media_url = media_url
        self.signals = WorkerSignals()
        self._stop_event = threading.Event()
        self.resource_manager = ResourceManager()
        self.control_point = None
    
    def stop(self):
        self._stop_event.set()
    
    def should_stop(self):
        return self._stop_event.is_set()
    
    def run(self):
        try:
            self.signals.status_changed.emit(f"正在连接 {self.device.name}...")
            logger.info(f"开始连接: {self.device}")
            
            self.control_point = DLNAControlPoint(self.device)
            self.control_point.set_av_transport_uri(self.media_url)
            self.control_point.play()
            
            self.signals.status_changed.emit(f"投屏运行中: {self.device.name}")
            logger.info(f"投屏开始: {self.device}")
            
            while not self.should_stop():
                time.sleep(1)
                
        except Exception as e:
            logger.error(f"投屏错误: {str(e)}", exc_info=True)
            self.signals.error_occurred.emit(f"投屏失败: {str(e)}")
        finally:
            self.cleanup()
            self.signals.operation_completed.emit()
    
    def cleanup(self):
        try:
            if self.control_point:
                self.control_point.stop()
        except Exception as e:
            logger.error(f"停止失败: {str(e)}")
        
        self.resource_manager.cleanup()
        self.signals.status_changed.emit("投屏已停止")

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

class DLNAMirrorApp(QMainWindow):
    """主窗口"""
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DLNA投屏工具")
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
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        queue_handler = logging.handlers.QueueHandler(self.log_queue)
        queue_handler.setLevel(logging.INFO)
        queue_handler.setFormatter(formatter)
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
                devices = UPNPDiscoverer.discover()
                self.discovered_devices = devices
                
                # 按设备类型分组
                type_groups = {}
                for device in devices:
                    if device.device_type not in type_groups:
                        type_groups[device.device_type] = []
                    type_groups[device.device_type].append(device)
                
                # 更新UI
                for dev_type, dev_list in type_groups.items():
                    type_item = QTreeWidgetItem([DLNADevice._get_friendly_type(dev_type), "", ""])
                    type_item.setData(0, Qt.UserRole, None)
                    
                    for device in dev_list:
                        dev_item = QTreeWidgetItem([
                            str(device),
                            DLNADevice._get_friendly_type(device.device_type),
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
                logger.error(f"发现设备失败: {str(e)}")
                self._set_status(MirrorStatus.ERROR, f"发现设备失败: {str(e)}")
        
        threading.Thread(target=discover_task, daemon=True).start()
    
    def get_selected_device(self) -> Optional[DLNADevice]:
        item = self.device_tree.currentItem()
        return item.data(0, Qt.UserRole) if item else None
    
    def show_device_details(self):
        device = self.get_selected_device()
        if device:
            DeviceInfoDialog(device, self).exec_()
    
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
        
        self.current_worker = DLNAMirrorWorker(device, media_url)
        self.current_worker.signals.status_changed.connect(
            lambda s: self._set_status(MirrorStatus.MIRRORING, s))
        self.current_worker.signals.error_occurred.connect(
            lambda e: (QMessageBox.critical(self, "错误", e),
                      self._set_status(MirrorStatus.ERROR, e)))
        self.current_worker.signals.operation_completed.connect(
            self._on_mirroring_finished)
        
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.refresh_btn.setEnabled(False)
        self.info_btn.setEnabled(False)
        
        self.current_worker.start()
    
    def stop_mirroring(self):
        if self.current_status not in [MirrorStatus.MIRRORING, MirrorStatus.CONNECTING]:
            return
        
        self._set_status(MirrorStatus.STOPPING, "正在停止...")
        self.stop_btn.setEnabled(False)
        
        if self.current_worker:
            self.current_worker.stop()
        
        QTimer.singleShot(5000, self._force_stop_mirroring)
    
    def _force_stop_mirroring(self):
        if self.current_status == MirrorStatus.STOPPING:
            logger.warning("强制停止")
            self._on_mirroring_finished()
    
    def _on_mirroring_finished(self):
        if self.current_worker:
            self.current_worker = None
        
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
            record = self.log_queue.get()
            # 将LogRecord对象格式化为字符串
            if isinstance(record, logging.LogRecord):
                msg = self.log_display.document().defaultFont().family() + ' - ' + record.getMessage()
                self.log_display.appendPlainText(msg)
            else:
                self.log_display.appendPlainText(str(record))

    
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