"""Constants for Motorola Nursery."""

DOMAIN = "motorola_nursery"
PLATFORMS = [
    "camera",
    "sensor",
    "binary_sensor",
    "number",
    "select",
    "switch",
    "button",
    "media_player",
    "event",
]

CONF_SID_USER_ID = "sid_user_id"
CONF_SID_DEVICE = "sid_device"
# Configuration field names, not embedded credentials.
CONF_MAGIC_TOKEN = "magic_token"  # nosec B105
CONF_RTSP_USERNAME = "rtsp_username"
CONF_RTSP_PASSWORD = "rtsp_password"  # nosec B105
CONF_ACCESS_TOKEN = "access_token"  # nosec B105

CONF_SESSION = "cloud_session"
CONF_EMAIL = "email"
CONF_CODE = "code"
CONF_DEVICE = "device"
CONF_LOCAL_CONTROL = "local_control"
CONF_USE_STATIC_RTSP_PORT = "use_static_rtsp_port"
CONF_RTSP_PORT = "rtsp_port"
