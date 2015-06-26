import importlib

import six

class PropertyHolder(object):
    pass

def objectize(dct):
    ph = PropertyHolder()

    for k, v in six.iteritems(dct):
        setattr(ph, k, v)

    return ph

def import_class(name):
    (module_name, class_name) = name.rsplit('.', 1)

    module = importlib.import_module(module_name)
    return getattr(module, class_name)
