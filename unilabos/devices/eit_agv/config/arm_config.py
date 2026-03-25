# arm_config.py
# -*- coding: utf-8 -*-
"""
机械臂驱动配置文件
"""

# 机械臂控制器连接配置
ARM_HOST = "127.0.0.1"
ARM_PORT = 6101
ARM_TIMEOUT = 3000  # 单位: 毫秒

# 夹爪控制配置
# 是否启用夹持到检测功能: True表示夹爪闭合后检测是否夹到物料, False表示不检测
ENABLE_GRIP_DETECTION = True
