"""來源适配器集合。"""
from .ccms import CCMSAdapter
from .nss import NSSAdapter
from .schoolweb import SchoolWebAdapter

ADAPTERS = {"nss": NSSAdapter, "ccms": CCMSAdapter, "schoolweb": SchoolWebAdapter}
