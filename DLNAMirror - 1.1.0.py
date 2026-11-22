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
from typing import Optional, Dict, List, Tuple
import requests
import pygame
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                            QHBoxLayout, QPushButton, QLabel, QComboBox, 
                            QMessageBox, QGroupBox, QPlainTextEdit, QTreeWidget,
                            QTreeWidgetItem, QHeaderView,QLineEdit)
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
    """资源管理类，确保资源正确释放"""
    def __init__(self):
        self._resources = []
    
    def add(self, resource, cleanup_func):
        """添加需要管理的资源"""
        self._resources.append((resource, cleanup_func))
        return resource
    
    def cleanup(self):
        """清理所有资源"""
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
    video_frame = pyqtSignal(object)  # 用于传输视频帧

class DLNADevice:
    """DLNA设备表示类"""
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
        """解析设备描述文档获取详细信息"""
        try:
            response = requests.get(self.service_url, timeout=3)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            
            # 解析设备信息
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
        """获取用于显示的设备信息"""
        return {
            "名称": self.name,
            "制造商": self.manufacturer,
            "型号": f"{self.model_name} {self.model_number}",
            "序列号": self.serial_number,
            "类型": self.device_type,
            "IP地址": f"{self.ip}:{self.port}",
            "服务URL": self.service_url,
            "唯一标识": self.udn
        }
    
    def __str__(self):
        return f"{self.name} ({self.manufacturer} {self.model_name})"

class UPNPDiscoverer:
    """UPnP设备发现类"""
    @staticmethod
    def discover(timeout=3) -> List[DLNADevice]:
        """发现局域网内的UPnP设备"""
        devices = []
        seen_devices = set()
        
        # UPnP发现消息
        msg = (
            'M-SEARCH * HTTP/1.1\r\n'
            'HOST: 239.255.255.250:1900\r\n'
            'MAN: "ssdp:discover"\r\n'
            'MX: 3\r\n'
            'ST: ssdp:all\r\n'  # 发现所有设备
            '\r\n'
        ).encode('utf-8')
        
        # 创建UDP套接字
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        
        try:
            # 发送发现请求
            sock.sendto(msg, ('239.255.255.250', 1900))
            
            # 设置超时
            sock.settimeout(timeout)
            
            # 收集响应
            while True:
                try:
                    data, addr = sock.recvfrom(4096)
                    device = UPNPDiscoverer._parse_response(data, addr)
                    if device and device.service_url not in seen_devices:
                        seen_devices.add(device.service_url)
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
    def _parse_response(data: bytes, addr: Tuple[str, int]) -> Optional[DLNADevice]:
        """解析UPnP响应"""
        try:
            headers = {}
            lines = data.decode('utf-8').split('\r\n')
            
            # 解析头部
            for line in lines[1:]:  # 跳过第一行
                if ':' in line:
                    key, value = line.split(':', 1)
                    headers[key.strip().upper()] = value.strip()
            
            # 检查必要字段
            if 'LOCATION' not in headers or 'SERVER' not in headers:
                return None
                
            # 提取设备信息
            location = headers['LOCATION']
            server_info = headers['SERVER']
            device_type = headers.get('ST', 'unknown')
            
            # 从server信息中提取设备类型
            if 'MediaRenderer' in server_info:
                device_type = 'MediaRenderer'
            elif 'MediaServer' in server_info:
                device_type = 'MediaServer'
                
            parsed = urlparse(location)
            
            # 获取设备名称
            name = headers.get('USN', '').split('::')[0]
            if not name:
                name = parsed.hostname
                
            return DLNADevice(
                name=name,
                ip=parsed.hostname,
                port=parsed.port,
                service_url=location,
                device_type=device_type
            )
            
        except Exception as e:
            logger.warning(f"解析设备响应失败: {str(e)}")
            return None

class DLNAControlPoint:
    """DLNA控制点实现"""
    def __init__(self, device: DLNADevice):
        self.device = device
        self.session_id = ""
        self.av_transport_service = None
        self.rendering_control_service = None
        self._find_services()
    
    def _find_services(self):
        """查找需要的服务"""
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
        
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            f'<u:{action} xmlns:u="{service["type"]}">'
        )
        
        for key, value in params.items():
            body += f'<{key}>{value}</{key}>'
        
        body += (
            f'</u:{action}>'
            '</s:Body>'
            '</s:Envelope>'
        )
        
        try:
            response = requests.post(control_url, data=body, headers=headers, timeout=5)
            response.raise_for_status()
            return ET.fromstring(response.content)
        except Exception as e:
            logger.error(f"SOAP请求失败: {str(e)}")
            raise
    
    def set_av_transport_uri(self, uri: str, meta_data: str = ""):
        """设置播放URI"""
        params = {
            'InstanceID': '0',
            'CurrentURI': uri,
            'CurrentURIMetaData': meta_data
        }
        return self._send_soap_request(self.av_transport_service, 'SetAVTransportURI', params)
    
    def play(self):
        """开始播放"""
        params = {
            'InstanceID': '0',
            'Speed': '1'
        }
        return self._send_soap_request(self.av_transport_service, 'Play', params)
    
    def stop(self):
        """停止播放"""
        params = {
            'InstanceID': '0'
        }
        return self._send_soap_request(self.av_transport_service, 'Stop', params)
    
    def get_volume(self):
        """获取音量"""
        params = {
            'InstanceID': '0',
            'Channel': 'Master'
        }
        response = self._send_soap_request(self.rendering_control_service, 'GetVolume', params)
        if response is not None:
            return response.findtext(".//CurrentVolume", "0")
        return "0"
    
    def set_volume(self, volume: int):
        """设置音量"""
        params = {
            'InstanceID': '0',
            'Channel': 'Master',
            'DesiredVolume': str(volume)
        }
        return self._send_soap_request(self.rendering_control_service, 'SetVolume', params)

class DLNAMirrorWorker(threading.Thread):
    """DLNA投屏工作线程"""
    def __init__(self, device: DLNADevice, media_url: str):
        super().__init__(daemon=True)
        self.device = device
        self.media_url = media_url
        self.signals = WorkerSignals()
        self._stop_event = threading.Event()
        self.resource_manager = ResourceManager()
        self.timeout = 10  # 秒
        self.control_point = None
    
    def stop(self):
        """安全停止线程"""
        self._stop_event.set()
    
    def should_stop(self):
        """检查是否应该停止"""
        return self._stop_event.is_set()
    
    def run(self):
        """主工作流程"""
        try:
            self.signals.status_changed.emit(f"正在连接 {self.device.name}...")
            logger.info(f"开始DLNA投屏连接: {self.device}")
            
            # 初始化控制点
            self.control_point = DLNAControlPoint(self.device)
            
            # 设置媒体URI
            self.control_point.set_av_transport_uri(self.media_url)
            
            # 开始播放
            self.control_point.play()
            
            self.signals.status_changed.emit(f"DLNA投屏运行中: {self.device.name}")
            logger.info(f"DLNA投屏开始: {self.device}")
            
            # 监控播放状态
            while not self.should_stop():
                time.sleep(1)
            
        except Exception as e:
            logger.error(f"DLNA投屏错误: {str(e)}", exc_info=True)
            self.signals.error_occurred.emit(f"DLNA投屏失败: {str(e)}")
        finally:
            self.cleanup()
            self.signals.operation_completed.emit()
    
    def cleanup(self):
        """资源清理"""
        try:
            if self.control_point:
                self.control_point.stop()
        except Exception as e:
            logger.error(f"停止播放失败: {str(e)}")
        
        self.resource_manager.cleanup()
        self.signals.status_changed.emit("DLNA投屏已停止")

class DeviceInfoDialog(QMessageBox):
    """设备信息对话框"""
    def __init__(self, device: DLNADevice, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设备详细信息")
        self.setIcon(QMessageBox.Information)
        
        info = device.get_display_info()
        text = "\n".join(f"{key}: {value}" for key, value in info.items())
        self.setText(text)
        
        if device.icon_url:
            self.addButton("查看设备图标", QMessageBox.ActionRole)
            self.buttonClicked.connect(lambda: QDesktopServices.openUrl(QUrl(device.icon_url)))
        
        self.addButton("关闭", QMessageBox.AcceptRole)

class DLNAMirrorApp(QMainWindow):
    """主应用程序窗口"""
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DLNA投屏工具 (完整版)")
        self.setWindowIcon(QIcon("dlna_mirror.png"))
        self.setMinimumSize(QSize(800, 600))
        
        # 状态变量
        self.current_status = MirrorStatus.IDLE
        self.current_worker: Optional[DLNAMirrorWorker] = None
        self.discovered_devices: List[DLNADevice] = []
        self.log_queue = queue.Queue()
        
        # 初始化UI
        self.init_ui()
        
        # 启动日志更新定时器
        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self.update_log_display)
        self.log_timer.start(500)  # 每500ms更新一次日志
        
        # 添加日志处理器
        self._setup_logging()
        
        # 初始设备发现
        self.refresh_devices()
    
    def init_ui(self):
        """初始化用户界面"""
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout()
        main_widget.setLayout(main_layout)
        
        # 设备发现组
        discover_group = QGroupBox("设备发现与控制")
        discover_layout = QVBoxLayout()
        
        # 设备树形列表
        self.device_tree = QTreeWidget()
        self.device_tree.setHeaderLabels(["设备名称", "类型", "IP地址"])
        self.device_tree.setColumnCount(3)
        self.device_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.device_tree.setSelectionMode(QTreeWidget.SingleSelection)
        self.device_tree.itemDoubleClicked.connect(self.show_device_details)
        
        # 控制按钮
        control_layout = QHBoxLayout()
        self.refresh_btn = QPushButton("刷新设备列表")
        self.refresh_btn.clicked.connect(self.refresh_devices)
        
        self.info_btn = QPushButton("查看设备详情")
        self.info_btn.clicked.connect(self.show_device_details)
        self.info_btn.setEnabled(False)
        
        self.start_btn = QPushButton("开始投屏")
        self.start_btn.clicked.connect(self.start_mirroring)
        self.start_btn.setEnabled(False)
        
        self.stop_btn = QPushButton("停止投屏")
        self.stop_btn.clicked.connect(self.stop_mirroring)
        self.stop_btn.setEnabled(False)
        
        control_layout.addWidget(self.refresh_btn)
        control_layout.addWidget(self.info_btn)
        control_layout.addWidget(self.start_btn)
        control_layout.addWidget(self.stop_btn)
        
        # 媒体URL输入
        media_layout = QHBoxLayout()
        media_layout.addWidget(QLabel("媒体URL:"))
        self.media_url_input = QLineEdit("http://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4")
        media_layout.addWidget(self.media_url_input)
        
        discover_layout.addWidget(self.device_tree)
        discover_layout.addLayout(control_layout)
        discover_layout.addLayout(media_layout)
        discover_group.setLayout(discover_layout)
        
        # 状态显示
        self.status_label = QLabel("状态: 等待开始")
        self.status_label.setAlignment(Qt.AlignCenter)
        
        # 日志显示
        self.log_display = QPlainTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setMaximumBlockCount(1000)
        
        # 添加到主布局
        main_layout.addWidget(discover_group)
        main_layout.addWidget(self.status_label)
        main_layout.addWidget(QLabel("运行日志:"))
        main_layout.addWidget(self.log_display)
    
    def _setup_logging(self):
        """设置日志处理"""
        class QueueHandler(logging.Handler):
            def __init__(self, queue):
                super().__init__()
                self.queue = queue
            
            def emit(self, record):
                self.queue.put(self.format(record))
        
        queue_handler = QueueHandler(self.log_queue)
        queue_handler.setLevel(logging.INFO)
        logger.addHandler(queue_handler)

    
    def refresh_devices(self):
        """刷新设备列表"""
        if self.current_status != MirrorStatus.IDLE:
            QMessageBox.warning(self, "警告", "请先停止当前投屏")
            return
        
        self._set_status(MirrorStatus.DISCOVERING)
        self.start_btn.setEnabled(False)
        self.info_btn.setEnabled(False)
        self.device_tree.clear()
        
        # 在新线程中执行设备发现
        def discover_task():
            try:
                devices = UPNPDiscoverer.discover()
                self.discovered_devices = devices
                
                # 按设备类型分组
                device_groups = {}
                for device in devices:
                    if device.device_type not in device_groups:
                        device_groups[device.device_type] = []
                    device_groups[device.device_type].append(device)
                
                # 在主线程更新UI
                for device_type, devices_in_group in device_groups.items():
                    group_item = QTreeWidgetItem([device_type, "", ""])
                    group_item.setData(0, Qt.UserRole, None)  # 标记为分组项
                    
                    for device in devices_in_group:
                        item = QTreeWidgetItem([str(device), device.device_type, f"{device.ip}:{device.port}"])
                        item.setData(0, Qt.UserRole, device)  # 存储设备对象
                        group_item.addChild(item)
                    
                    self.device_tree.addTopLevelItem(group_item)
                    group_item.setExpanded(True)
                
                if devices:
                    self._set_status(MirrorStatus.IDLE, f"找到 {len(devices)} 个DLNA设备")
                    self.start_btn.setEnabled(True)
                    self.info_btn.setEnabled(True)
                else:
                    self._set_status(MirrorStatus.IDLE, "未找到DLNA设备")
                
            except Exception as e:
                logger.error(f"设备发现错误: {str(e)}", exc_info=True)
                self._set_status(MirrorStatus.ERROR, f"设备发现错误: {str(e)}")
        
        threading.Thread(target=discover_task, daemon=True).start()
    
    def get_selected_device(self) -> Optional[DLNADevice]:
        """获取当前选中的设备"""
        item = self.device_tree.currentItem()
        if item and item.data(0, Qt.UserRole):
            return item.data(0, Qt.UserRole)
        return None
    
    def show_device_details(self):
        """显示设备详细信息"""
        device = self.get_selected_device()
        if device:
            dialog = DeviceInfoDialog(device, self)
            dialog.exec_()
    
    def start_mirroring(self):
        """开始投屏"""
        if self.current_status != MirrorStatus.IDLE:
            return
        
        device = self.get_selected_device()
        if not device:
            QMessageBox.warning(self, "警告", "请选择有效的DLNA设备")
            return
        
        media_url = self.media_url_input.text().strip()
        if not media_url:
            QMessageBox.warning(self, "警告", "请输入有效的媒体URL")
            return
        
        self._set_status(MirrorStatus.CONNECTING, f"正在连接 {device.name}...")
        
        # 创建工作线程
        self.current_worker = DLNAMirrorWorker(device, media_url)
        self.current_worker.signals.status_changed.connect(
            lambda s: self._set_status(MirrorStatus.MIRRORING, s))
        self.current_worker.signals.error_occurred.connect(
            lambda e: (QMessageBox.critical(self, "错误", e),
                      self._set_status(MirrorStatus.ERROR, e)))
        self.current_worker.signals.operation_completed.connect(
            self._on_mirroring_finished)
        
        # 更新UI状态
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.refresh_btn.setEnabled(False)
        self.info_btn.setEnabled(False)
        
        # 启动工作线程
        self.current_worker.start()
    
    def stop_mirroring(self):
        """停止投屏"""
        if self.current_status not in [MirrorStatus.MIRRORING, MirrorStatus.CONNECTING]:
            return
        
        self._set_status(MirrorStatus.STOPPING, "正在停止投屏...")
        self.stop_btn.setEnabled(False)
        
        if self.current_worker:
            self.current_worker.stop()
        
        # 设置超时强制停止
        QTimer.singleShot(5000, self._force_stop_mirroring)
    
    def _force_stop_mirroring(self):
        """强制停止投屏（超时后调用）"""
        if self.current_status == MirrorStatus.STOPPING:
            logger.warning("强制停止投屏线程")
            self._on_mirroring_finished()
    
    def _on_mirroring_finished(self):
        """投屏完成或停止后的清理"""
        if self.current_worker:
            self.current_worker.cleanup()
            self.current_worker = None
        
        # 重置UI状态
        self._set_status(MirrorStatus.IDLE, "投屏已停止")
        self.start_btn.setEnabled(len(self.discovered_devices) > 0)
        self.stop_btn.setEnabled(False)
        self.refresh_btn.setEnabled(True)
        self.info_btn.setEnabled(True)
    
    def _set_status(self, status: MirrorStatus, message: Optional[str] = None):
        """更新状态"""
        self.current_status = status
        if message:
            self.status_label.setText(f"状态: {message}")
    
    def update_log_display(self):
        """更新日志显示"""
        while not self.log_queue.empty():
            try:
                record = self.log_queue.get()
                if isinstance(record, str):
                    self.log_display.appendPlainText(record)
                else:
                    self.log_display.appendPlainText(str(record))
            except Exception as e:
                logger.error(f"日志显示错误: {str(e)}")
                break

    
    def closeEvent(self, event):
        """处理窗口关闭事件"""
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
            # 停止日志定时器
            self.log_timer.stop()
            event.accept()

def check_dependencies():
    """检查系统依赖"""
    missing = []
    
    try:
        import pygame
    except ImportError:
        missing.append("pygame")
    
    try:
        import PyQt5
    except ImportError:
        missing.append("PyQt5")
    
    try:
        import requests
    except ImportError:
        missing.append("requests")
    
    if missing:
        print("缺少必要的Python模块:", ", ".join(missing))
        print("请执行: pip install", " ".join(missing))
        return False
    
    return True

def main():
    """应用程序入口"""
    if not check_dependencies():
        return 1
    
    app = QApplication(sys.argv)
    
    try:
        window = DLNAMirrorApp()
        window.show()
        return app.exec_()
    except Exception as e:
        logger.critical(f"应用程序错误: {str(e)}", exc_info=True)
        QMessageBox.critical(None, "致命错误", f"应用程序遇到错误:\n{str(e)}")
        return 1
    finally:
        logger.info("应用程序退出")

if __name__ == "__main__":
    sys.exit(main())