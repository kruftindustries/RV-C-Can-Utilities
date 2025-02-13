import sys
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QProgressBar, QLabel, QFrame, QTabWidget,
                             QComboBox, QPushButton, QSpinBox, QFormLayout)
from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QPalette, QColor

#Windows: python -m pip install gs-usb==0.3.0

from gs_usb.gs_usb import GsUsb
from gs_usb.gs_usb_frame import GsUsbFrame
from gs_usb.constants import CAN_EFF_FLAG, CAN_ERR_FLAG, CAN_RTR_FLAG
import threading
import queue

# Constants from RV-C documentation
TANK_STATUS_DGN = 0x19FFB7AF
TANK_CALIBRATION_DGN = 0x19FFB6AF  # From RV-C doc, with priority 6 in high byte. Last byte AF might be different in your device, this code does not perform an address claim so you will have to monitor CAN traffic.
GS_USB_NONE_ECHO_ID = 0xFFFFFFFF

GS_USB_ECHO_ID = 0
GS_CAN_MODE_NORMAL = 0
GS_CAN_MODE_LISTEN_ONLY = (1 << 0)
GS_CAN_MODE_LOOP_BACK = (1 << 1)
GS_CAN_MODE_ONE_SHOT = (1 << 3)
GS_CAN_MODE_HW_TIMESTAMP = (1 << 4)

debug = False  # Set to true to display the measured tank level on RX

class TankWidget(QFrame):
    def __init__(self, name, color, parent=None):
        super().__init__(parent)
        self.setFrameStyle(QFrame.Box | QFrame.Raised)
        self.setLineWidth(2)
        
        layout = QVBoxLayout()
        
        # Tank name label
        self.name_label = QLabel(name)
        self.name_label.setAlignment(Qt.AlignCenter)
        
        # Progress bar for tank level
        self.level_bar = QProgressBar()
        self.level_bar.setOrientation(Qt.Vertical)
        self.level_bar.setRange(0, 100)
        self.level_bar.setTextVisible(True)
        self.level_bar.setFormat("%p%")
        
        # Set progress bar color
        palette = self.level_bar.palette()
        palette.setColor(QPalette.Highlight, color)
        self.level_bar.setPalette(palette)
        
        # Level label
        self.level_label = QLabel("N/A L")
        self.level_label.setAlignment(Qt.AlignCenter)
        
        layout.addWidget(self.name_label)
        layout.addWidget(self.level_bar, 1)
        layout.addWidget(self.level_label)
        
        self.setLayout(layout)
        self.setMinimumWidth(100)
        self.setMinimumHeight(300)

    def update_level(self, relative_level, absolute_level):
        self.level_bar.setValue(int(relative_level))
        self.level_label.setText(f"{int(relative_level)}%")

class CalibrationTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        
        layout = QFormLayout()
        
        # Tank selection dropdown
        self.tank_select = QComboBox()
        self.tank_select.addItems([
            "Fresh Water (0)",
            "Black Waste (1)",
            "Grey Waste (2)",
            "LPG (3)"
        ])
        
        # Level input
        self.level_input = QSpinBox()
        self.level_input.setRange(0, 100)
        self.level_input.setSuffix("%")
        
        # Calibration button
        self.calibrate_button = QPushButton("Send Calibration")
        self.calibrate_button.clicked.connect(self.send_calibration)
        
        # Add widgets to layout
        layout.addRow("Select Tank:", self.tank_select)
        layout.addRow("Relative Level:", self.level_input)
        layout.addRow(self.calibrate_button)
        
        self.setLayout(layout)
        
        # Get USB device
        self.dev = None
        try:
            devs = GsUsb.scan()
            if devs:
                self.dev = devs[0]
        except Exception as e:
            print(f"Error initializing USB device: {e}")

    def send_calibration(self):
        if not self.dev:
            print("No USB device available")
            return
            
        try:
            # Get selected tank instance
            tank_instance = self.tank_select.currentIndex()
            level = self.level_input.value()
            
            # Create calibration command frame
            # According to RV-C doc 6.28.3:
            # Byte 0: Instance
            # Byte 1: Relative level
            # Byte 2: Resolution (using 1)
            # Bytes 3-4: Absolute level (using 0)
            # Bytes 5-6: Tank size (using 0)
            data = bytearray([
                tank_instance,  # Instance
                level,         # Relative level
                0xFF,            # Resolution
                0xFF, 0xFF,         # Absolute level (2 bytes)
                0xFF, 0xFF          # Tank size (2 bytes)
            ])
            
            # Create and send frame
            frame = GsUsbFrame(
                can_id=TANK_CALIBRATION_DGN | CAN_EFF_FLAG,
                data=bytes(data)
            )
            
            if self.dev.send(frame):
                print(f"Sent calibration command for tank {tank_instance} at {level}%")
            else:
                print("Failed to send calibration command")
                
        except Exception as e:
            print(f"Error sending calibration command: {e}")

class TankMonitor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("RV Tank Monitor")
        
        # Create tab widget
        self.tab_widget = QTabWidget()
        self.setCentralWidget(self.tab_widget)
        
        # Monitor tab
        self.monitor_tab = QWidget()
        monitor_layout = QHBoxLayout()
        
        # Create tank widgets with different colors
        self.tanks = {
            0: TankWidget("Fresh Water", QColor(0, 128, 255)),
            1: TankWidget("Black Waste", QColor(64, 64, 64)),
            2: TankWidget("Grey Waste", QColor(128, 128, 128)),
            3: TankWidget("LPG", QColor(255, 165, 0))
        }
        
        # Add tanks to layout
        for tank in self.tanks.values():
            monitor_layout.addWidget(tank)
        
        self.monitor_tab.setLayout(monitor_layout)
        
        # Add calibration tab
        self.calibration_tab = CalibrationTab()
        
        # Add tabs to widget
        self.tab_widget.addTab(self.monitor_tab, "Monitor")
        self.tab_widget.addTab(self.calibration_tab, "Calibration")
        
        # Message queue for thread communication
        self.message_queue = queue.Queue()
        
        # Start CAN monitoring thread
        self.can_thread = threading.Thread(target=self.monitor_can, daemon=True)
        self.can_thread.start()
        
        # Timer to update GUI
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.process_messages)
        self.update_timer.start(100)  # Update every 100ms

    def monitor_can(self):
        """CAN bus monitoring thread"""
        try:
            devs = GsUsb.scan()
            if not devs:
                print("No GS_USB device found")
                return
                
            dev = devs[0]
            dev.stop()
            
            if not dev.set_bitrate(250000):
                print("Failed to set bitrate")
                return
                
            dev.start(GS_CAN_MODE_NORMAL)
            
            while True:
                frame = GsUsbFrame()
                if dev.read(frame, 1):
                    if (frame.can_id & ~CAN_EFF_FLAG) == TANK_STATUS_DGN and \
                       frame.echo_id == GS_USB_NONE_ECHO_ID:
                        self.message_queue.put(frame)
                        
        except Exception as e:
            print(f"CAN monitoring error: {e}")

    @Slot()
    def process_messages(self):
        """Process CAN messages from queue and update GUI"""
        try:
            while not self.message_queue.empty():
                frame = self.message_queue.get_nowait()
                
                instance = frame.data[0]
                relative_level = frame.data[1]
                resolution = frame.data[2] if frame.data[2] > 0 else 1
                absolute_level = int.from_bytes(frame.data[3:5], 'little')
                
                
                if (debug):
                    level_percent = (14 / resolution) * 100
                    print(relative_level)
                else:
                    level_percent = (relative_level / resolution) * 100
                
                if instance in self.tanks:
                    self.tanks[instance].update_level(level_percent, absolute_level)
                    
        except queue.Empty:
            pass
        except Exception as e:
            print(f"Error processing message: {e}")
            

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    window = TankMonitor()
    window.show()
    sys.exit(app.exec())