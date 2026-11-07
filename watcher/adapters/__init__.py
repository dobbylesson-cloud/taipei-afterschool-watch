"""來源适配器集合。"""
from .ccms import CCMSAdapter
from .nss import NSSAdapter

ADAPTERS = {"nss": NSSAdapter, "ccms": CCMSAdapter}
