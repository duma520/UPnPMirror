import os
import sys
import logging
import logging.handlers
import threading
import time
import queue
import socket
import struct
from urllib.parse import urlparse
from datetime import datetime
from enum import Enum, auto
from typing import Optional, Dict, List, Tuple
import pygame
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                            QHBoxLayout, QPushButton, QLabel, QComboBox, 
                            QMessageBox, QGroupBox, QPlainTextEdit)
from PyQt5.QtCore import Qt, QSize, QObject, pyqtSignal, QTimer
from PyQt5.QtGui import QIcon

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
        return resource  # 方便链式调用
    
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

class DLNADevice:
    """DLNA设备表示类"""
    def __init__(self, name: str, ip: str, port: int, service_url: str):
        self.name = name
        self.ip = ip
        self.port = port
        self.service_url = service_url
        self.supported_actions = []
    
    def __str__(self):
        return f"{self.name} ({self.ip}:{self.port})"

    # 获取更友好的设备描述
    def get_friendly_description(self):
        """获取更友好的设备描述"""
        # 尝试从设备名称中提取制造商信息
        manufacturer = ""
        if "manufacturer" in self.name.lower():
            parts = self.name.split(" ")
            if len(parts) > 1:
                manufacturer = parts[0]
        
        # 创建描述
        desc = f"{self.name} (IP: {self.ip})"
        if manufacturer:
            desc = f"[{manufacturer}] {desc}"
        
        return desc



class UPNPDiscoverer:
    """UPnP设备发现类"""
    @staticmethod
    def discover(timeout=3) -> List[DLNADevice]:
        """发现局域网内的UPnP设备"""
        devices = []
        
        # UPnP发现消息
        msg = (
            'M-SEARCH * HTTP/1.1\r\n'
            'HOST: 239.255.255.250:1900\r\n'
            'MAN: "ssdp:discover"\r\n'
            'MX: 3\r\n'
            'ST: upnp:rootdevice\r\n'
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
                    if device:
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
            if 'LOCATION' not in headers:
                return None
                
            # 提取设备信息
            location = headers['LOCATION']
            parsed = urlparse(location)
            
            # 获取设备名称
            name = headers.get('USN', '').split('::')[0]
            if not name:
                name = parsed.hostname
                
            return DLNADevice(
                name=name,
                ip=parsed.hostname,
                port=parsed.port,
                service_url=location
            )
            
        except Exception as e:
            logger.warning(f"解析设备响应失败: {str(e)}")
            return None

class DLNAMirrorWorker(threading.Thread):
    """DLNA投屏工作线程"""
    def __init__(self, device: DLNADevice):
        super().__init__(daemon=True)
        self.device = device
        self.signals = WorkerSignals()
        self._stop_event = threading.Event()
        self.resource_manager = ResourceManager()
        self.timeout = 10  # 秒
    
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
            
            # 初始化PyGame
            pygame.init()
            self.resource_manager.add(pygame, lambda p: pygame.quit())
            
            # 创建显示窗口
            screen = self.resource_manager.add(
                pygame.display.set_mode((800, 600)),
                lambda s: pygame.display.quit()
            )
            pygame.display.set_caption(f"DLNA投屏 - {self.device.name}")
            
            # 模拟媒体流（实际应替换为真实DLNA流）
            self._simulate_stream(screen)
            
        except Exception as e:
            logger.error(f"DLNA投屏错误: {str(e)}", exc_info=True)
            self.signals.error_occurred.emit(f"DLNA投屏失败: {str(e)}")
        finally:
            self.cleanup()
            self.signals.operation_completed.emit()
    
    def _simulate_stream(self, screen):
        """模拟视频流（实际项目中应替换为真实DLNA流处理）"""
        self.signals.status_changed.emit(f"DLNA投屏运行中: {self.device.name}")
        logger.info(f"模拟DLNA投屏开始: {self.device}")
        
        clock = pygame.time.Clock()
        font = pygame.font.SysFont(None, 48)
        start_time = time.time()
        
        while not self.should_stop():
            try:
                # 清屏
                screen.fill((0, 0, 0))
                
                # 显示模拟信息
                elapsed = time.time() - start_time
                text = font.render(
                    f"模拟DLNA投屏: {self.device.name}",
                    True, (255, 255, 255))
                screen.blit(text, (50, 50))
                
                text = font.render(
                    f"运行时间: {elapsed:.1f}秒",
                    True, (255, 255, 255))
                screen.blit(text, (50, 120))
                
                # 更新显示
                pygame.display.flip()
                clock.tick(30)
                
                # 处理事件
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        self.stop()
                
            except pygame.error as e:
                logger.warning(f"显示错误: {str(e)}")
                break
    
    def cleanup(self):
        """资源清理"""
        self.resource_manager.cleanup()
        self.signals.status_changed.emit("DLNA投屏已停止")

class DLNAMirrorApp(QMainWindow):
    """主应用程序窗口"""
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DLNA投屏工具")
        self.setWindowIcon(QIcon("dlna_mirror.png"))
        self.setMinimumSize(QSize(600, 500))
        
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
        discover_group = QGroupBox("设备发现")
        discover_layout = QVBoxLayout()
        
        self.devices_combo = QComboBox()
        self.refresh_btn = QPushButton("刷新设备列表")
        self.refresh_btn.clicked.connect(self.refresh_devices)
        
        discover_layout.addWidget(QLabel("可用DLNA设备:"))
        discover_layout.addWidget(self.devices_combo)
        discover_layout.addWidget(self.refresh_btn)
        discover_group.setLayout(discover_layout)
        
        # 控制按钮
        control_layout = QHBoxLayout()
        self.start_btn = QPushButton("开始投屏")
        self.start_btn.clicked.connect(self.start_mirroring)
        self.stop_btn = QPushButton("停止投屏")
        self.stop_btn.clicked.connect(self.stop_mirroring)
        self.stop_btn.setEnabled(False)
        
        control_layout.addWidget(self.start_btn)
        control_layout.addWidget(self.stop_btn)
        
        # 状态显示
        self.status_label = QLabel("状态: 等待开始")
        self.status_label.setAlignment(Qt.AlignCenter)
        
        # 日志显示
        self.log_display = QPlainTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setMaximumBlockCount(1000)  # 限制日志行数
        
        # 添加到主布局
        main_layout.addWidget(discover_group)
        main_layout.addLayout(control_layout)
        main_layout.addWidget(self.status_label)
        main_layout.addWidget(QLabel("运行日志:"))
        main_layout.addWidget(self.log_display)
    
    def _setup_logging(self):
        """设置日志处理"""
        queue_handler = logging.handlers.QueueHandler(self.log_queue)
        queue_handler.setLevel(logging.INFO)
        logger.addHandler(queue_handler)
    
    def refresh_devices(self):
        """刷新设备列表"""
        if self.current_status != MirrorStatus.IDLE:
            QMessageBox.warning(self, "警告", "请先停止当前投屏")
            return
        
        self._set_status(MirrorStatus.DISCOVERING)
        self.start_btn.setEnabled(False)
        self.devices_combo.clear()
        
        # 在新线程中执行设备发现
        def discover_task():
            try:
                devices = UPNPDiscoverer.discover()
                self.discovered_devices = devices
                device_descriptions = [d.get_friendly_description() for d in devices]
                # 改进设备显示方式
                device_descriptions = []
                for device in devices:
                    # 提取设备制造商信息（如果存在）
                    manufacturer = ""
                    if "manufacturer" in device.name.lower():
                        parts = device.name.split(" ")
                        if len(parts) > 1:
                            manufacturer = parts[0]
                    
                    # 创建更友好的设备描述
                    desc = f"{device.name} (IP: {device.ip})"
                    if manufacturer:
                        desc = f"[{manufacturer}] {desc}"
                    
                    device_descriptions.append(desc)
                
                # 在主线程更新UI
                self.devices_combo.addItems(device_descriptions)
                if devices:
                    self._set_status(MirrorStatus.IDLE, f"找到 {len(devices)} 个DLNA设备")
                    self.start_btn.setEnabled(True)
                else:
                    self._set_status(MirrorStatus.IDLE, "未找到DLNA设备")
                
            except Exception as e:
                logger.error(f"设备发现错误: {str(e)}", exc_info=True)
                self._set_status(MirrorStatus.ERROR, f"设备发现错误: {str(e)}")
        
        threading.Thread(target=discover_task, daemon=True).start()
    
    def start_mirroring(self):
        """开始投屏"""
        if self.current_status != MirrorStatus.IDLE:
            return
        
        selected_index = self.devices_combo.currentIndex()
        if selected_index < 0 or selected_index >= len(self.discovered_devices):
            QMessageBox.warning(self, "警告", "请选择有效的DLNA设备")
            return
        
        selected_device = self.discovered_devices[selected_index]
        self._set_status(MirrorStatus.CONNECTING, f"正在连接 {selected_device.name}...")
        
        # 创建工作线程
        self.current_worker = DLNAMirrorWorker(selected_device)
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
    
    def _set_status(self, status: MirrorStatus, message: Optional[str] = None):
        """更新状态"""
        self.current_status = status
        if message:
            self.status_label.setText(f"状态: {message}")
    
    def update_log_display(self):
        """更新日志显示"""
        while not self.log_queue.empty():
            record = self.log_queue.get()
            self.log_display.appendPlainText(record)
    
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