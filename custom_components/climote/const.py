import logging

DOMAIN = "climote"
LOGGER = logging.getLogger(__package__)

# URL Paths
BASE_URL = "https://climote.climote.ie"
PATH_SECURITY_CHECK = "/index/check-user-security"
PATH_LOGIN = "/manager/login"
PATH_INDEX = "/manager/index"
PATH_STATUS = "/manager/get-status"
PATH_WAIT_STATUS = "/manager/waiting-get-status-response"
PATH_BOOST = "/manager/boost"
PATH_DELIVERY_REPORT = "/manager/wait-for-delivery-report"
PATH_SCHEDULE = "/manager/get-heating-schedule"

# Configuration Keys
CONF_EMAIL = "email"
CONF_DEVICE_ID = "device_id"   # Climote Device No. — the 10-digit hub identifier
CONF_PIN = "pin"
CONF_POLL_INTERVAL = "poll_interval"
CONF_BOOST_DURATION = "boost_duration"

# Defaults
DEFAULT_POLL_INTERVAL = 1800  # 30 minutes in seconds
DEFAULT_BOOST_DURATION = 1.0   # 1 hour
MIN_POLL_INTERVAL = 1800       # 30 minutes minimum to protect GSM limits
