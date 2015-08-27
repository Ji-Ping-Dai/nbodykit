"""
    Declare PluginMount and various extention points.

    To define a Plugin, set __metaclass__ to PluginMount, and
    define a .register member.

"""

class PluginMount(type):
    
    def __init__(cls, name, bases, attrs):

        # only executes when processing the mount point itself.
        if not hasattr(cls, 'plugins'):
            cls.plugins = []
        # called for each plugin, which already has 'plugins' list
        else:
            # track names of classes
            cls.plugins.append(cls)
            
            # try to call register class method
            if hasattr(cls, 'register'):
                cls.register()

def BoxSizeParser(value):
    """
    Parse a string of either a single float, or 
    a space-separated string of 3 floats, representing 
    a box size. Designed to be used by the Painter plugins
    
    Returns
    -------
    BoxSize : array_like
        an array of size 3 giving the box size in each dimension
    """
    boxsize = numpy.empty(3, dtype='f8')
    sizes = map(float, value.split())
    if len(sizes) == 1: sizes = sizes[0]
    boxsize[:] = sizes
    return boxsize

class InputPainter:
    """
    Mount point for plugins which refer to the reading of input files 
    and the subsequent painting of those fields.

    Plugins implementing this reference should provide the following 
    attributes:

    field_type : str
        class attribute giving the name of the subparser which 
        defines the necessary command line arguments for the plugin
    
    register : classmethod
        A class method taking no arguments that adds a subparser
        and the necessary command line arguments for the plugin
    
    paint : method
        A method that performs the painting of the field. It 
        takes the following arguments:
            pm : pypm.particlemesh.ParticleMesh

    read: method
        A method that performs the reading of the field. It shall
        returns the position (in 0 to BoxSize) and velocity (in the
        same units as position), in chunks as an iterator.

    """
    __metaclass__ = PluginMount
    
    from argparse import ArgumentParser
    parser = ArgumentParser("", add_help=False)
    subparsers = parser.add_subparsers()
    field_type = None

    def __init__(self, dict):
        self.__dict__.update(dict)

    @classmethod
    def parse(kls, string): 
        words = string.split(':')
        
        ns = kls.parser.parse_args(words)
        klass = ns.klass
        d = ns.__dict__
        # break the cycle
        del d['klass']
        d['string'] = string
        painter = klass(d)
        return painter

    def __eq__(self, other):
        return self.string == other.string

    def __ne__(self, other):
        return self.string != other.string

    def read(self, columns, comm):
        return NotImplemented    

    def paint(self, pm):
        pm.real[:] = 0
        Ntot = 0

        chunks = self.read(['Position', 'Mass'], pm.comm)

        for chunk in chunks:
            position = chunk['Position']
            weight = chunk['Mass']

            layout = pm.decompose(position)
            position = layout.exchange(position)
            if weight is None:
                Ntot += len(position)
                weight = 1
            else:
                weight = layout.exchange(weight)
                Ntot += weight.sum()
            pm.paint(position, weight)
        return pm.comm.allreduce(Ntot)

    @classmethod
    def add_parser(kls, name):
        return kls.subparsers.add_parser(name, 
                usage=None, add_help=False, formatter_class=HelpFormatterColon)
    
    @classmethod
    def format_help(kls):
        
        rt = []
        for plugin in kls.plugins:
            k = plugin.field_type
            rt.append(kls.subparsers.choices[k].format_help())

        if not len(rt):
            return "No available input field types"
        else:
            return '\n'.join(rt)

#------------------------------------------------------------------------------
import sys
import contextlib

class PowerSpectrumStorage:
    __metaclass__ = PluginMount

    field_type = None
    klasses = {}

    def __init__(self, path):
        self.path = path

    @classmethod
    def add_storage_klass(kls, klass):
        kls.klasses[klass.field_type] = klass

    @classmethod
    def get(kls, dim, path):
        klass = kls.klasses[dim]
        obj = klass(path)
        return obj
        
    @contextlib.contextmanager
    def open(self):
        if self.path and self.path != '-':
            ff = open(self.path, 'w')
        else:
            ff = sys.stdout
            
        try:
            yield ff
        finally:
            if ff is not sys.stdout:
                ff.close()

    def write(self, data, **meta):
        return NotImplemented

from argparse import RawTextHelpFormatter
class HelpFormatterColon(RawTextHelpFormatter):
    """ This class is used to format the ':' seperated usage strings """
    def _format_usage(self, usage, actions, groups, prefix):
        if prefix is None:
            prefix = 'usage: '

        # this stripped down version supports no groups
        assert len(groups) == 0

        prog = '%(prog)s' % dict(prog=self._prog)

        # split optionals from positionals
        optionals = []
        positionals = []
        for action in actions:
            if action.option_strings:
                optionals.append(action)
            else:
                positionals.append(action)

        # build full usage string
        format = self._format_actions_usage
        action_usage = format(positionals + optionals, groups)
        usage = ''.join([s for s in [prog, action_usage] if s])
        # prefix with 'usage:'
        return '%s%s\n\n' % (prefix, usage)

    def _format_actions_usage(self, actions, groups):
        # collect all actions format strings
        parts = []
        for i, action in enumerate(actions):

            # produce all arg strings
            if not action.option_strings:
                part = self._format_args(action, action.dest)

                part = ':' + part

                # add the action string to the list
                parts.append(part)

            # produce the first way to invoke the option in brackets
            else:
                option_string = action.option_strings[0]

                # if the Optional doesn't take a value, format is:
                #    -s or --long
                if action.nargs == 0:
                    part = '%s' % option_string

                # if the Optional takes a value, format is:
                #    -s ARGS or --long ARGS
                else:
                    default = action.dest.upper()
                    args_string = self._format_args(action, default)
                    part = '%s %s' % (option_string, args_string)

                # make it look optional if it's not required or in a group
                if not action.required:
                    part = '[:%s]' % part

                # add the action string to the list
                parts.append(part)

        # join all the action items with spaces
        text = ''.join([item for item in parts if item is not None])

        # return the text
        return text

            
#------------------------------------------------------------------------------          
import os.path

def load(filename, namespace=None):
    """ An adapter for ArgumentParser to load a plugin.
        
        Parameters
        ----------
        filename : string
            path to the .py file
        namespace : dict
            global namespace, if None, an empty space will be created
        
        Returns
        -------
        namespace : dict
            modified global namespace of the plugin script.
    """
    if namespace is None:
        namespace = {}
    if os.path.isdir(filename):
        # FIXME: walk the dir and load all .py files.
        raise ValueError("Can not load directory")
    try:
        execfile(filename, namespace)
    except Exception as e:
        raise RuntimeError("Failed to load plugin '%s': %s" % (filename, str(e)))
    return namespace

builtins = ['TPMSnapshotPainter', 'HaloFilePainter', 'PandasPainter',
            'PlainTextPainter', 'Power1DStorage', 'Power2DStorage']
for plugin in builtins:
    globals().update(load(os.path.join(os.path.dirname(__file__), plugin + '.py')))
 
