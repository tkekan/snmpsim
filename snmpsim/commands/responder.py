#
# This file is part of snmpsim software.
#
# Copyright (c) 2010-2019, Ilya Etingof <etingof@gmail.com>
# License: http://snmplabs.com/snmpsim/license.html
#
# SNMP Agent Simulator
#
import getopt
import os
import stat
import sys
import traceback
from hashlib import md5

from pyasn1 import debug as pyasn1_debug
from pyasn1.codec.ber import decoder
from pyasn1.codec.ber import encoder
from pyasn1.compat.octets import null
from pyasn1.compat.octets import str2octs
from pyasn1.error import PyAsn1Error
from pyasn1.type import univ
from pysnmp import debug as pysnmp_debug
from pysnmp import error
from pysnmp.carrier.asyncore.dgram import udp
from pysnmp.carrier.asyncore.dgram import udp6
from pysnmp.carrier.asyncore.dgram import unix
from pysnmp.carrier.asyncore.dispatch import AsyncoreDispatcher
from pysnmp.entity import config
from pysnmp.entity import engine
from pysnmp.entity.rfc3413 import cmdrsp
from pysnmp.entity.rfc3413 import context
from pysnmp.proto import api
from pysnmp.proto import rfc1902
from pysnmp.proto import rfc1905
from pysnmp.smi import exval, indices
from pysnmp.smi.error import MibOperationError

from snmpsim import confdir
from snmpsim import daemon
from snmpsim import log
from snmpsim.error import NoDataNotification
from snmpsim.error import SnmpsimError
from snmpsim.record import dump
from snmpsim.record import mvc
from snmpsim.record import sap
from snmpsim.record import snmprec
from snmpsim.record import walk
from snmpsim.record.search.database import RecordIndex
from snmpsim.record.search.file import getRecord
from snmpsim.record.search.file import searchRecordByOid

PROGRAM_NAME = os.path.basename(sys.argv[0])

AUTH_PROTOCOLS = {
    'MD5': config.usmHMACMD5AuthProtocol,
    'SHA': config.usmHMACSHAAuthProtocol,
    'SHA224': config.usmHMAC128SHA224AuthProtocol,
    'SHA256': config.usmHMAC192SHA256AuthProtocol,
    'SHA384': config.usmHMAC256SHA384AuthProtocol,
    'SHA512': config.usmHMAC384SHA512AuthProtocol,
    'NONE': config.usmNoAuthProtocol
}

PRIV_PROTOCOLS = {
  'DES': config.usmDESPrivProtocol,
  '3DES': config.usm3DESEDEPrivProtocol,
  'AES': config.usmAesCfb128Protocol,
  'AES128': config.usmAesCfb128Protocol,
  'AES192': config.usmAesCfb192Protocol,
  'AES192BLMT': config.usmAesBlumenthalCfb192Protocol,
  'AES256': config.usmAesCfb256Protocol,
  'AES256BLMT': config.usmAesBlumenthalCfb256Protocol,
  'NONE': config.usmNoPrivProtocol
}

RECORD_TYPES = {
    dump.DumpRecord.ext: dump.DumpRecord(),
    mvc.MvcRecord.ext: mvc.MvcRecord(),
    sap.SapRecord.ext: sap.SapRecord(),
    walk.WalkRecord.ext: walk.WalkRecord(),
}

SELF_LABEL = 'self'

HELP_MESSAGE = """\
Usage: %s [--help]
    [--version ]
    [--debug=<%s>]
    [--debug-asn1=<%s>]
    [--daemonize]
    [--process-user=<uname>] [--process-group=<gname>]
    [--pid-file=<file>]
    [--logging-method=<%s[:args>]>]
    [--log-level=<%s>]
    [--cache-dir=<dir>]
    [--variation-modules-dir=<dir>]
    [--variation-module-options=<module[=alias][:args]>] 
    [--force-index-rebuild]
    [--validate-data]
    [--args-from-file=<file>]
    [--transport-id-offset=<number>]
    [--v2c-arch]
    [--v3-only]
    [--v3-engine-id=<hexvalue>]
    [--v3-context-engine-id=<hexvalue>]
    [--v3-user=<username>]
    [--v3-auth-key=<key>]
    [--v3-auth-proto=<%s>]
    [--v3-priv-key=<key>]
    [--v3-priv-proto=<%s>]
    [--data-dir=<dir>]
    [--max-varbinds=<number>]
    [--agent-udpv4-endpoint=<X.X.X.X:NNNNN>]
    [--agent-udpv6-endpoint=<[X:X:..X]:NNNNN>]
    [--agent-unix-endpoint=</path/to/named/pipe>]""" % (
    sys.argv[0],
    '|'.join([x for x in getattr(pysnmp_debug, 'FLAG_MAP', getattr(pysnmp_debug, 'flagMap', ()))
              if x != 'mibview']),
    '|'.join([x for x in getattr(pyasn1_debug, 'FLAG_MAP', getattr(pyasn1_debug, 'flagMap', ()))]),
    '|'.join(log.METHODS_MAP),
    '|'.join(log.LEVELS_MAP),
    '|'.join(sorted([x for x in AUTH_PROTOCOLS if x != 'NONE'])),
    '|'.join(sorted([x for x in PRIV_PROTOCOLS if x != 'NONE']))
)


class TransportEndpointsBase:
    def __init__(self):
        self.__endpoint = None

    def add(self, addr):
        self.__endpoint = self._addEndpoint(addr)
        return self

    def _addEndpoint(self, addr):
        raise NotImplementedError()

    def __len__(self):
        return len(self.__endpoint)

    def __getitem__(self, i):
        return self.__endpoint[i]


class IPv4TransportEndpoints(TransportEndpointsBase):
    def _addEndpoint(self, addr):
        f = lambda h, p=161: (h, int(p))

        try:
            h, p = f(*addr.split(':'))

        except Exception:
            raise SnmpsimError('improper IPv4/UDP endpoint %s' % addr)

        return udp.UdpTransport().openServerMode((h, p)), addr


class IPv6TransportEndpoints(TransportEndpointsBase):
    def _addEndpoint(self, addr):
        if not udp6:
            raise SnmpsimError('This system does not support UDP/IP6')

        if addr.find(']:') != -1 and addr[0] == '[':
            h, p = addr.split(']:')

            try:
                h, p = h[1:], int(p)

            except Exception:
                raise SnmpsimError('improper IPv6/UDP endpoint %s' % addr)

        elif addr[0] == '[' and addr[-1] == ']':
            h, p = addr[1:-1], 161

        else:
            h, p = addr, 161

        return udp6.Udp6Transport().openServerMode((h, p)), addr


class UnixTransportEndpoints(TransportEndpointsBase):
    def _addEndpoint(self, addr):
        if not unix:
            raise SnmpsimError(
                'This system does not support UNIX domain sockets')

        return unix.UnixTransport().openServerMode(addr), addr


# Extended snmprec record handler

class SnmprecRecordMixIn(object):

    def evaluateValue(self, oid, tag, value, **context):
        # Variation module reference
        if ':' in tag:
            modName, tag = tag[tag.index(':')+1:], tag[:tag.index(':')]

        else:
            modName = None

        if modName:
            if ('variationModules' in context and
                    modName in context['variationModules']):

                if 'dataValidation' in context:
                    return oid, tag, univ.Null

                else:
                    if context['setFlag']:

                        hexvalue = self.grammar.hexifyValue(
                            context['origValue'])

                        if hexvalue is not None:
                            context['hexvalue'] = hexvalue
                            context['hextag'] = self.grammar.getTagByType(
                                context['origValue'])
                            context['hextag'] += 'x'

                    # prepare agent and record contexts on first reference
                    (variationModule,
                     agentContexts,
                     recordContexts) = context['variationModules'][modName]

                    if context['dataFile'] not in agentContexts:
                        agentContexts[context['dataFile']] = {}

                    if context['dataFile'] not in recordContexts:
                        recordContexts[context['dataFile']] = {}

                    variationModule['agentContext'] = agentContexts[context['dataFile']]

                    recordContexts = recordContexts[context['dataFile']]

                    if oid not in recordContexts:
                        recordContexts[oid] = {}

                    variationModule['recordContext'] = recordContexts[oid]

                    handler = variationModule['variate']

                    # invoke variation module
                    oid, tag, value = handler(oid, tag, value, **context)

            else:
                raise SnmpsimError(
                    'Variation module "%s" referenced but not '
                    'loaded\r\n' % modName)

        if not modName:
            if 'dataValidation' in context:
                snmprec.SnmprecRecord.evaluateValue(
                    self, oid, tag, value, **context)

            if (not context['nextFlag'] and
                    not context['exactMatch'] or context['setFlag']):
                return context['origOid'], tag, context['errorStatus']

        if not hasattr(value, 'tagSet'):  # not already a pyasn1 object
            return snmprec.SnmprecRecord.evaluateValue(
                       self, oid, tag, value, **context)

        return oid, tag, value

    def evaluate(self, line, **context):
        oid, tag, value = self.grammar.parse(line)

        oid = self.evaluateOid(oid)

        if context.get('oidOnly'):
            value = None

        else:
            try:
                oid, tag, value = self.evaluateValue(oid, tag, value, **context)

            except NoDataNotification:
                raise

            except MibOperationError:
                raise

            except PyAsn1Error as exc:
                raise SnmpsimError(
                    'value evaluation for %s = %r failed: '
                    '%s\r\n' % (oid, value, exc))

        return oid, value


class SnmprecRecord(SnmprecRecordMixIn, snmprec.SnmprecRecord):
    pass


RECORD_TYPES[SnmprecRecord.ext] = SnmprecRecord()


class CompressedSnmprecRecord(SnmprecRecordMixIn,
                              snmprec.CompressedSnmprecRecord):
    pass


RECORD_TYPES[CompressedSnmprecRecord.ext] = CompressedSnmprecRecord()


class AbstractLayout:
    layout = '?'


class DataFile(AbstractLayout):
    layout = 'text'
    openedQueue = []
    maxQueueEntries = 31  # max number of open text and index files

    def __init__(self, textFile, textParser, variationModules):
        self.__recordIndex = RecordIndex(textFile, textParser)
        self.__textParser = textParser
        self.__textFile = os.path.abspath(textFile)
        self._variationModules = variationModules
        
    def indexText(self, forceIndexBuild=False, validateData=False):
        self.__recordIndex.create(forceIndexBuild, validateData)
        return self

    def close(self):
        self.__recordIndex.close()
    
    def getHandles(self):
        if not self.__recordIndex.isOpen():
            if len(DataFile.openedQueue) > self.maxQueueEntries:
                log.info('Closing %s' % self)
                DataFile.openedQueue[0].close()
                del DataFile.openedQueue[0]

            DataFile.openedQueue.append(self)

            log.info('Opening %s' % self)

        return self.__recordIndex.getHandles()

    def processVarBinds(self, varBinds, **context):
        rspVarBinds = []

        if context.get('nextFlag'):
            errorStatus = exval.endOfMib

        else:
            errorStatus = exval.noSuchInstance

        try:
            text, db = self.getHandles()

        except SnmpsimError as exc:
            log.error(
                'Problem with data file or its index: %s' % exc)

            return [(vb[0], errorStatus) for vb in varBinds]

        varsRemaining = varsTotal = len(varBinds)

        log.info(
            'Request var-binds: %s, flags: %s, '
            '%s' % (', '.join(['%s=<%s>' % (vb[0], vb[1].prettyPrint())
                               for vb in varBinds]),
                    context.get('nextFlag') and 'NEXT' or 'EXACT',
                    context.get('setFlag') and 'SET' or 'GET'))

        for oid, val in varBinds:
            textOid = str(univ.OctetString('.'.join(['%s' % x for x in oid])))

            try:
                line = self.__recordIndex.lookup(
                    str(univ.OctetString('.'.join(['%s' % x for x in oid]))))

            except KeyError:
                offset = searchRecordByOid(oid, text, self.__textParser)
                subtreeFlag = exactMatch = False

            else:
                offset, subtreeFlag, prevOffset = line.split(str2octs(','), 2)
                subtreeFlag, exactMatch = int(subtreeFlag), True

            offset = int(offset)

            text.seek(offset)

            varsRemaining -= 1

            line, _, _ = getRecord(text)  # matched line
 
            while True:
                if exactMatch:
                    if context.get('nextFlag') and not subtreeFlag:

                        _nextLine, _, _ = getRecord(text)  # next line

                        if _nextLine:
                            _nextOid, _ = self.__textParser.evaluate(
                                _nextLine, oidOnly=True)

                            try:
                                _, subtreeFlag, _ = self.__recordIndex.lookup(
                                    str(_nextOid)).split(str2octs(','), 2)

                            except KeyError:
                                log.error(
                                    'data error for %s at %s, index '
                                    'broken?' % (self, _nextOid))
                                line = ''  # fatal error

                            else:
                                subtreeFlag = int(subtreeFlag)
                                line = _nextLine

                        else:
                            line = _nextLine

                else:  # search function above always rounds up to the next OID
                    if line:
                        _oid, _ = self.__textParser.evaluate(
                            line, oidOnly=True
                        )

                    else:  # eom
                        _oid = 'last'

                    try:
                        _, _, _prevOffset = self.__recordIndex.lookup(
                            str(_oid)).split(str2octs(','), 2)

                    except KeyError:
                        log.error(
                            'data error for %s at %s, index '
                            'broken?' % (self, _oid))
                        line = ''  # fatal error

                    else:
                        _prevOffset = int(_prevOffset)

                        # previous line serves a subtree?
                        if _prevOffset >= 0:
                            text.seek(_prevOffset)
                            _prevLine, _, _ = getRecord(text)
                            _prevOid, _ = self.__textParser.evaluate(
                                _prevLine, oidOnly=True)

                            if _prevOid.isPrefixOf(oid):
                                # use previous line to the matched one
                                line = _prevLine
                                subtreeFlag = True

                if not line:
                    _oid = oid
                    _val = errorStatus
                    break

                callContext = context.copy()
                callContext.update(
                    (),
                    origOid=oid, 
                    origValue=val,
                    dataFile=self.__textFile,
                    subtreeFlag=subtreeFlag,
                    exactMatch=exactMatch,
                    errorStatus=errorStatus,
                    varsTotal=varsTotal,
                    varsRemaining=varsRemaining,
                    variationModules=self._variationModules
                )
 
                try:
                    _oid, _val = self.__textParser.evaluate(
                        line, **callContext)

                    if _val is exval.endOfMib:
                        exactMatch = True
                        subtreeFlag = False
                        continue

                except NoDataNotification:
                    raise

                except MibOperationError:
                    raise

                except Exception as exc:
                    _oid = oid
                    _val = errorStatus
                    log.error(
                        'data error at %s for %s: %s' % (self, textOid, exc))

                break

            rspVarBinds.append((_oid, _val))

        log.info(
            'Response var-binds: %s' % (
                ', '.join(['%s=<%s>' % (
                    vb[0], vb[1].prettyPrint()) for vb in rspVarBinds])))

        return rspVarBinds
 
    def __str__(self):
        return '%s controller' % self.__textFile


# Collect data files

def getDataFiles(tgtDir, topLen=None):
    if topLen is None:
        topLen = len(tgtDir.split(os.path.sep))

    dirContent = []

    for dFile in os.listdir(tgtDir):
        fullPath = os.path.join(tgtDir, dFile)

        inode = os.lstat(fullPath)

        if stat.S_ISLNK(inode.st_mode):
            relPath = fullPath.split(os.path.sep)[topLen:]
            fullPath = os.readlink(fullPath)

            if not os.path.isabs(fullPath):
                fullPath = os.path.join(tgtDir, fullPath)

            inode = os.stat(fullPath)

        else:
            relPath = fullPath.split(os.path.sep)[topLen:]

        if stat.S_ISDIR(inode.st_mode):
            dirContent += getDataFiles(fullPath, topLen)
            continue

        if not stat.S_ISREG(inode.st_mode):
            continue

        for dExt in RECORD_TYPES:
            if dFile.endswith(dExt):
                break

        else:
            continue

        # just the file name would serve for agent identification
        if relPath[0] == SELF_LABEL:
            relPath = relPath[1:]

        if len(relPath) == 1 and relPath[0] == SELF_LABEL + os.path.extsep + dExt:
            relPath[0] = relPath[0][4:]

        ident = os.path.join(*relPath)
        ident = ident[:-len(dExt) - 1]
        ident = ident.replace(os.path.sep, '/')

        dirContent.append(
            (fullPath,
             RECORD_TYPES[dExt],
             ident)
        )

    return dirContent


# Lightweight MIB instrumentation (API-compatible with pysnmp's)

class MibInstrumController:
    def __init__(self, dataFile):
        self.__dataFile = dataFile

    def __str__(self):
        return str(self.__dataFile)

    def __getCallContext(self, acInfo, nextFlag=False, setFlag=False):
        if acInfo is None:
            return {'nextFlag': nextFlag,
                    'setFlag': setFlag}

        acFun, snmpEngine = acInfo  # we injected snmpEngine object earlier

        # this API is first introduced in pysnmp 4.2.6
        execCtx = snmpEngine.observer.getExecutionContext(
                'rfc3412.receiveMessage:request')

        (transportDomain,
         transportAddress,
         securityModel,
         securityName,
         securityLevel,
         contextName,
         pduType) = (execCtx['transportDomain'],
                     execCtx['transportAddress'],
                     execCtx['securityModel'],
                     execCtx['securityName'],
                     execCtx['securityLevel'],
                     execCtx['contextName'],
                     execCtx['pdu'].getTagSet())

        log.info(
            'SNMP EngineID %s, transportDomain %s, transportAddress %s, '
            'securityModel %s, securityName %s, securityLevel '
            '%s' % (hasattr(snmpEngine, 'snmpEngineID') and
                    snmpEngine.snmpEngineID.prettyPrint() or '<unknown>',
                    transportDomain, transportAddress, securityModel,
                    securityName, securityLevel))

        return {'snmpEngine': snmpEngine,
                'transportDomain': transportDomain,
                'transportAddress': transportAddress,
                'securityModel': securityModel,
                'securityName': securityName,
                'securityLevel': securityLevel,
                'contextName': contextName,
                'nextFlag': nextFlag,
                'setFlag': setFlag}

    def readVars(self, varBinds, acInfo=None):
        return self.__dataFile.processVarBinds(
                varBinds, **self.__getCallContext(acInfo, False))

    def readNextVars(self, varBinds, acInfo=None):
        return self.__dataFile.processVarBinds(
                varBinds, **self.__getCallContext(acInfo, True))

    def writeVars(self, varBinds, acInfo=None):
        return self.__dataFile.processVarBinds(
                varBinds, **self.__getCallContext(acInfo, False, True))


# Data files index as a MIB instrumentation in a dedicated SNMP context

class DataIndexInstrumController:
    indexSubOid = (1,)

    def __init__(self, baseOid=(1, 3, 6, 1, 4, 1, 20408, 999)):
        self.__db = indices.OidOrderedDict()
        self.__indexOid = baseOid + self.indexSubOid
        self.__idx = 1

    def __str__(self):
        return '<index> controller'

    def readVars(self, varBinds, acInfo=None):
        return [(vb[0], self.__db.get(vb[0], exval.noSuchInstance))
                for vb in varBinds]

    def __getNextVal(self, key, default):
        try:
            key = self.__db.nextKey(key)

        except KeyError:
            return key, default

        else:
            return key, self.__db[key]
                                                            
    def readNextVars(self, varBinds, acInfo=None):
        return [self.__getNextVal(vb[0], exval.endOfMib)
                for vb in varBinds]

    def writeVars(self, varBinds, acInfo=None):
        return [(vb[0], exval.noSuchInstance)
                for vb in varBinds]
    
    def addDataFile(self, *args):
        for idx in range(len(args)):
            self.__db[
                self.__indexOid + (idx+1, self.__idx)
                ] = rfc1902.OctetString(args[idx])
        self.__idx += 1


mibInstrumControllerSet = {
    DataFile.layout: MibInstrumController
}


# Suggest variations of context name based on request data
def probeContext(transportDomain, transportAddress,
                 contextEngineId, contextName):
    if contextEngineId:
        candidate = [
            contextEngineId, contextName, '.'.join(
                [str(x) for x in transportDomain])]

    else:
        # try legacy layout w/o contextEnginId in the path
        candidate = [
            contextName, '.'.join(
                [str(x) for x in transportDomain])]

    if transportDomain[:len(udp.domainName)] == udp.domainName:
        candidate.append(transportAddress[0])

    elif udp6 and transportDomain[:len(udp6.domainName)] == udp6.domainName:
        candidate.append(str(transportAddress[0]).replace(':', '_'))

    elif unix and transportDomain[:len(unix.domainName)] == unix.domainName:
        candidate.append(transportAddress)

    candidate = [str(x) for x in candidate if x]

    while candidate:
        yield rfc1902.OctetString(
            os.path.normpath(
                os.path.sep.join(candidate)).replace(os.path.sep, '/')).asOctets()
        del candidate[-1]

    # try legacy layout w/o contextEnginId in the path
    if contextEngineId:
        for candidate in probeContext(
                transportDomain, transportAddress, None, contextName):
            yield candidate
 

def main():
    forceIndexBuild = False
    validateData = False
    maxVarBinds = 64
    transportIdOffset = 0
    v2cArch = False
    v3Only = False
    pidFile = '/var/run/snmpsim/%s.pid' % PROGRAM_NAME
    foregroundFlag = True
    procUser = procGroup = None
    loggingMethod = ['stderr']
    loggingLevel = None
    variationModulesOptions = {}
    variationModules = {}

    try:
        opts, params = getopt.getopt(
            sys.argv[1:], 'hv',
            ['help', 'version', 'debug=', 'debug-snmp=', 'debug-asn1=',
             'daemonize', 'process-user=', 'process-group=', 'pid-file=',
             'logging-method=', 'log-level=', 'device-dir=', 'cache-dir=',
             'variation-modules-dir=', 'force-index-rebuild',
             'validate-device-data', 'validate-data', 'v2c-arch', 'v3-only',
             'transport-id-offset=', 'variation-module-options=',
             'args-from-file=',
             # this option starts new SNMPv3 engine configuration
             'v3-engine-id=', 'v3-context-engine-id=', 'v3-user=',
             'v3-auth-key=', 'v3-auth-proto=',  'v3-priv-key=', 'v3-priv-proto=',
             'data-dir=', 'max-varbinds=', 'agent-udpv4-endpoint=',
             'agent-udpv6-endpoint=', 'agent-unix-endpoint='])

    except Exception as exc:
        sys.stderr.write('ERROR: %s\r\n%s\r\n' % (exc, HELP_MESSAGE))
        return 1

    if params:
        sys.stderr.write(
            'ERROR: extra arguments supplied %s\r\n'
            '%s\r\n' % (params, HELP_MESSAGE))
        return 1

    v3Args = []

    for opt in opts:
        if opt[0] == '-h' or opt[0] == '--help':
            sys.stderr.write("""\
Synopsis:
  SNMP Agents Simulation tool. Responds to SNMP requests, variate responses
  based on transport addresses, SNMP community name or SNMPv3 context name.
  Can implement highly complex behavior through variation modules.

Documentation:
  http://snmplabs.com/snmpsim/simulating-agents.html
%s
""" % HELP_MESSAGE)
            return 1

        if opt[0] == '-v' or opt[0] == '--version':
            import snmpsim
            import pysnmp
            import pyasn1

            sys.stderr.write("""\
SNMP Simulator version %s, written by Ilya Etingof <etingof@gmail.com>
Using foundation libraries: pysnmp %s, pyasn1 %s.
Python interpreter: %s
Software documentation and support at http://snmplabs.com/snmpsim
%s
""" % (snmpsim.__version__,
       getattr(pysnmp, '__version__', 'unknown'),
       getattr(pyasn1, '__version__', 'unknown'),
       sys.version, HELP_MESSAGE))
            return 1

        elif opt[0] in ('--debug', '--debug-snmp'):
            pysnmp_debug.setLogger(
                pysnmp_debug.Debug(
                    *opt[1].split(','),
                    **dict(loggerName='%s.pysnmp' % PROGRAM_NAME)))

        elif opt[0] == '--debug-asn1':
            pyasn1_debug.setLogger(
                pyasn1_debug.Debug(
                    *opt[1].split(','),
                    **dict(loggerName='%s.pyasn1' % PROGRAM_NAME)))

        elif opt[0] == '--daemonize':
            foregroundFlag = False

        elif opt[0] == '--process-user':
            procUser = opt[1]

        elif opt[0] == '--process-group':
            procGroup = opt[1]

        elif opt[0] == '--pid-file':
            pidFile = opt[1]

        elif opt[0] == '--logging-method':
            loggingMethod = opt[1].split(':')

        elif opt[0] == '--log-level':
            loggingLevel = opt[1]

        elif opt[0] in ('--device-dir', '--data-dir'):
            if [x for x in v3Args if x[0] in (
                    '--v3-engine-id', '--v3-context-engine-id')]:
                v3Args.append(opt)

            else:
                confdir.data.insert(0, opt[1])

        elif opt[0] == '--cache-dir':
            confdir.cache = opt[1]

        elif opt[0] == '--variation-modules-dir':
            confdir.variation.insert(0, opt[1])

        elif opt[0] == '--variation-module-options':
            args = opt[1].split(':', 1)

            try:
                modName, args = args[0], args[1]

            except Exception:
                sys.stderr.write(
                    'ERROR: improper variation module options: %s\r\n'
                    '%s\r\n' % (opt[1], HELP_MESSAGE))
                return 1

            if '=' in modName:
                modName, alias = modName.split('=', 1)

            else:
                alias = os.path.splitext(os.path.basename(modName))[0]

            if modName not in variationModulesOptions:
                variationModulesOptions[modName] = []

            variationModulesOptions[modName].append((alias, args))

        elif opt[0] == '--force-index-rebuild':
            forceIndexBuild = True

        elif opt[0] in ('--validate-device-data', '--validate-data'):
            validateData = True

        elif opt[0] == '--v2c-arch':
            v2cArch = True

        elif opt[0] == '--v3-only':
            v3Only = True

        elif opt[0] == '--transport-id-offset':
            try:
                transportIdOffset = max(0, int(opt[1]))

            except Exception as exc:
                sys.stderr.write(
                    'ERROR: %s\r\n%s\r\n' % (exc, HELP_MESSAGE))
                return 1

        # processing of the following args is postponed till SNMP engine startup

        elif opt[0] in ('--agent-udpv4-endpoint',
                        '--agent-udpv6-endpoint',
                        '--agent-unix-endpoint'):
            v3Args.append(opt)

        elif opt[0] in ('--agent-udpv4-endpoints-list',
                        '--agent-udpv6-endpoints-list',
                        '--agent-unix-endpoints-list'):
            sys.stderr.write(
                'ERROR: use --args-from-file=<file> option to list many '
                'endpoints\r\n%s\r\n' % HELP_MESSAGE)
            return 1

        elif opt[0] in ('--v3-engine-id', '--v3-context-engine-id',
                        '--v3-user', '--v3-auth-key', '--v3-auth-proto',
                        '--v3-priv-key', '--v3-priv-proto'):
            v3Args.append(opt)

        elif opt[0] == '--max-varbinds':
            try:
                if '--v3-engine-id' in [x[0] for x in v3Args]:
                    v3Args.append((opt[0], max(1, int(opt[1]))))

                else:
                    maxVarBinds = max(1, int(opt[1]))

            except Exception as exc:
                sys.stderr.write(
                    'ERROR: %s\r\n%s\r\n' % (exc, HELP_MESSAGE))
                return 1

        elif opt[0] == '--args-from-file':
            try:
                v3Args.extend(
                    [x.split('=', 1) for x in open(opt[1]).read().split()])

            except Exception as exc:
                sys.stderr.write(
                    'ERROR: file %s opening failure: %s\r\n'
                    '%s\r\n' % (opt[1], exc, HELP_MESSAGE))
                return 1

    if v2cArch and (
            v3Only or [x for x in v3Args if x[0][:4] == '--v3']):
        sys.stderr.write(
            'ERROR: either of --v2c-arch or --v3-* options should be used\r\n'
            '%s\r\n' % HELP_MESSAGE)
        return 1

    for opt in tuple(v3Args):
        if opt[0] == '--agent-udpv4-endpoint':
            try:
                v3Args.append((opt[0], IPv4TransportEndpoints().add(opt[1])))

            except Exception as exc:
                sys.stderr.write(
                    'ERROR: %s\r\n%s\r\n' % (exc, HELP_MESSAGE))
                return 1

        elif opt[0] == '--agent-udpv6-endpoint':
            try:
                v3Args.append((opt[0], IPv6TransportEndpoints().add(opt[1])))

            except Exception as exc:
                sys.stderr.write(
                    'ERROR: %s\r\n%s\r\n' % (exc, HELP_MESSAGE))
                return 1

        elif opt[0] == '--agent-unix-endpoint':
            try:
                v3Args.append((opt[0], UnixTransportEndpoints().add(opt[1])))

            except Exception as exc:
                sys.stderr.write(
                    'ERROR: %s\r\n%s\r\n' % (exc, HELP_MESSAGE))
                return 1

        else:
            v3Args.append(opt)

    if v3Args[:len(v3Args)//2] == v3Args[len(v3Args)//2:]:
        sys.stderr.write(
            'ERROR: agent endpoint address(es) not specified\r\n'
            '%s\r\n' % HELP_MESSAGE)
        return 1

    else:
        v3Args = v3Args[len(v3Args)//2:]

    with daemon.PrivilegesOf(procUser, procGroup):

        try:
            log.setLogger(PROGRAM_NAME, *loggingMethod, force=True)

            if loggingLevel:
                log.setLevel(loggingLevel)

        except SnmpsimError as exc:
            sys.stderr.write('%s\r\n%s\r\n' % (exc, HELP_MESSAGE))
            return 1

    if not foregroundFlag:
        try:
            daemon.daemonize(pidFile)

        except Exception as exc:
            sys.stderr.write(
                'ERROR: cant daemonize process: %s\r\n'
                '%s\r\n' % (exc, HELP_MESSAGE))
            return 1

    # hook up variation modules

    for variationModulesDir in confdir.variation:
        log.info(
            'Scanning "%s" directory for variation '
            'modules...' % variationModulesDir)

        if not os.path.exists(variationModulesDir):
            log.info('Directory "%s" does not exist' % variationModulesDir)
            continue

        for dFile in os.listdir(variationModulesDir):
            if dFile[-3:] != '.py':
                continue

            _toLoad = []

            modName = os.path.splitext(os.path.basename(dFile))[0]

            if modName in variationModulesOptions:
                while variationModulesOptions[modName]:
                    alias, args = variationModulesOptions[modName].pop()
                    _toLoad.append((alias, args))

                del variationModulesOptions[modName]

            else:
                _toLoad.append((modName, ''))

            mod = os.path.abspath(os.path.join(variationModulesDir, dFile))

            for alias, args in _toLoad:
                if alias in variationModules:
                    log.error(
                        'ignoring duplicate variation module "%s" at '
                        '"%s"' % (alias, mod))
                    continue

                ctx = {
                    'path': mod,
                    'alias': alias,
                    'args': args,
                    'moduleContext': {}
                }

                try:
                    if sys.version_info[0] > 2:
                        exec(compile(open(mod).read(), mod, 'exec'), ctx)

                    else:
                        execfile(mod, ctx)

                except Exception as exc:
                    log.error(
                        'Variation module "%s" execution failure: '
                        '%s' % (mod, exc))
                    return 1

                else:
                    # moduleContext, agentContexts, recordContexts
                    variationModules[alias] = ctx, {}, {}

        log.info('A total of %s modules found in '
                 '%s' % (len(variationModules), variationModulesDir))

    if variationModulesOptions:
        log.msg('WARNING: unused options for variation modules: '
                '%s' % ', '.join(variationModulesOptions))

    if not os.path.exists(confdir.cache):

        try:
            with daemon.PrivilegesOf(procUser, procGroup):
                os.makedirs(confdir.cache)

        except OSError as exc:
            log.error('failed to create cache directory "%s": '
                      '%s' % (confdir.cache, exc))
            return 1

        else:
            log.info('Cache directory "%s" created' % confdir.cache)

    if variationModules:
        log.info('Initializing variation modules...')

        for name, modulesContexts in variationModules.items():

            body = modulesContexts[0]

            for x in ('init', 'variate', 'shutdown'):
                if x not in body:
                    log.error('missing "%s" handler at variation module '
                              '"%s"' % (x, name))
                    return 1

            try:
                with daemon.PrivilegesOf(procUser, procGroup):
                    body['init'](options=body['args'], mode='variating')

            except Exception as exc:
                log.error(
                    'Variation module "%s" from "%s" load FAILED: '
                    '%s' % (body['alias'], body['path'], exc))

            else:
                log.info(
                    'Variation module "%s" from "%s" '
                    'loaded OK' % (body['alias'], body['path']))

    # Build pysnmp Managed Objects base from data files information

    def configureManagedObjects(
            dataDirs, dataIndexInstrumController, snmpEngine=None,
            snmpContext=None):

        _mibInstrums = {}
        _dataFiles = {}

        for dataDir in dataDirs:

            log.info(
                'Scanning "%s" directory for %s data '
                'files...' % (dataDir, ','.join([' *%s%s' % (os.path.extsep, x.ext)
                                                 for x in RECORD_TYPES.values()])))

            if not os.path.exists(dataDir):
                log.info('Directory "%s" does not exist' % dataDir)
                continue

            log.msg.incIdent()

            for fullPath, textParser, communityName in getDataFiles(dataDir):
                if communityName in _dataFiles:
                    log.error(
                        'ignoring duplicate Community/ContextName "%s" for data '
                        'file %s (%s already loaded)' % (communityName, fullPath,
                                                         _dataFiles[communityName]))
                    continue

                elif fullPath in _mibInstrums:
                    mibInstrum = _mibInstrums[fullPath]
                    log.info('Configuring *shared* %s' % (mibInstrum,))

                else:
                    dataFile = DataFile(fullPath, textParser, variationModules)
                    dataFile.indexText(forceIndexBuild, validateData)

                    mibInstrum = mibInstrumControllerSet[dataFile.layout](dataFile)

                    _mibInstrums[fullPath] = mibInstrum
                    _dataFiles[communityName] = fullPath

                    log.info('Configuring %s' % (mibInstrum,))

                log.info('SNMPv1/2c community name: %s' % (communityName,))

                if v2cArch:
                    contexts[univ.OctetString(communityName)] = mibInstrum

                    dataIndexInstrumController.addDataFile(
                        fullPath, communityName
                    )

                else:
                    agentName = md5(
                        univ.OctetString(communityName).asOctets()).hexdigest()

                    contextName = agentName

                    if not v3Only:
                        # snmpCommunityTable::snmpCommunityIndex can't be > 32
                        config.addV1System(
                            snmpEngine, agentName, communityName,
                            contextName=contextName)

                    snmpContext.registerContextName(contextName, mibInstrum)

                    if len(communityName) <= 32:
                        snmpContext.registerContextName(communityName, mibInstrum)

                    dataIndexInstrumController.addDataFile(
                        fullPath, communityName, contextName)

                    log.info(
                        'SNMPv3 Context Name: %s'
                        '%s' % (contextName, len(communityName) <= 32 and
                                ' or %s' % communityName or ''))

            log.msg.decIdent()

        del _mibInstrums
        del _dataFiles

    if v2cArch:

        def getBulkHandler(
                reqVarBinds, nonRepeaters, maxRepetitions, readNextVars):

            N = min(int(nonRepeaters), len(reqVarBinds))
            M = int(maxRepetitions)
            R = max(len(reqVarBinds)-N, 0)

            if R:
                M = min(M, maxVarBinds/R)

            if N:
                rspVarBinds = readNextVars(reqVarBinds[:N])

            else:
                rspVarBinds = []

            varBinds = reqVarBinds[-R:]

            while M and R:
                rspVarBinds.extend(readNextVars(varBinds))
                varBinds = rspVarBinds[-R:]
                M -= 1

            return rspVarBinds

        def commandResponderCbFun(
                transportDispatcher, transportDomain, transportAddress,
                wholeMsg):

            while wholeMsg:
                msgVer = api.decodeMessageVersion(wholeMsg)

                if msgVer in api.protoModules:
                    pMod = api.protoModules[msgVer]

                else:
                    log.error('Unsupported SNMP version %s' % (msgVer,))
                    return

                reqMsg, wholeMsg = decoder.decode(wholeMsg, asn1Spec=pMod.Message())

                communityName = reqMsg.getComponentByPosition(1)

                for candidate in probeContext(transportDomain, transportAddress,
                                              contextEngineId=SELF_LABEL,
                                              contextName=communityName):
                    if candidate in contexts:
                        log.info(
                            'Using %s selected by candidate %s; transport ID %s, '
                            'source address %s, context engine ID <empty>, '
                            'community name '
                            '"%s"' % (contexts[candidate], candidate,
                                      univ.ObjectIdentifier(transportDomain),
                                      transportAddress[0], communityName))
                        communityName = candidate
                        break

                else:
                    log.error(
                        'No data file selected for transport ID %s, source '
                        'address %s, community name '
                        '"%s"' % (univ.ObjectIdentifier(transportDomain),
                                  transportAddress[0], communityName))
                    return wholeMsg

                rspMsg = pMod.apiMessage.getResponse(reqMsg)
                rspPDU = pMod.apiMessage.getPDU(rspMsg)
                reqPDU = pMod.apiMessage.getPDU(reqMsg)

                if reqPDU.isSameTypeWith(pMod.GetRequestPDU()):
                    backendFun = contexts[communityName].readVars

                elif reqPDU.isSameTypeWith(pMod.SetRequestPDU()):
                    backendFun = contexts[communityName].writeVars

                elif reqPDU.isSameTypeWith(pMod.GetNextRequestPDU()):
                    backendFun = contexts[communityName].readNextVars

                elif (hasattr(pMod, 'GetBulkRequestPDU') and
                        reqPDU.isSameTypeWith(pMod.GetBulkRequestPDU())):

                    if not msgVer:
                        log.info(
                            'GETBULK over SNMPv1 from %s:%s' % (
                                transportDomain, transportAddress))
                        return wholeMsg

                    def backendFun(varBinds):
                        return getBulkHandler(
                            varBinds, pMod.apiBulkPDU.getNonRepeaters(reqPDU),
                            pMod.apiBulkPDU.getMaxRepetitions(reqPDU),
                            contexts[communityName].readNextVars
                        )

                else:
                    log.error(
                        'Unsupported PDU type %s from '
                        '%s:%s' % (reqPDU.__class__.__name__, transportDomain,
                                   transportAddress))
                    return wholeMsg

                try:
                    varBinds = backendFun(pMod.apiPDU.getVarBinds(reqPDU))

                except NoDataNotification:
                    return wholeMsg

                except Exception as exc:
                    log.error('Ignoring SNMP engine failure: %s' % exc)
                    return wholeMsg

                # Poor man's v2c->v1 translation
                errorMap = {
                    rfc1902.Counter64.tagSet: 5,
                    rfc1905.NoSuchObject.tagSet: 2,
                    rfc1905.NoSuchInstance.tagSet: 2,
                    rfc1905.EndOfMibView.tagSet: 2
                }

                if not msgVer:

                    for idx in range(len(varBinds)):

                        oid, val = varBinds[idx]

                        if val.tagSet in errorMap:
                            varBinds = pMod.apiPDU.getVarBinds(reqPDU)

                            pMod.apiPDU.setErrorStatus(
                                rspPDU, errorMap[val.tagSet])
                            pMod.apiPDU.setErrorIndex(
                                rspPDU, idx + 1)

                            break

                pMod.apiPDU.setVarBinds(rspPDU, varBinds)

                transportDispatcher.sendMessage(
                    encoder.encode(rspMsg), transportDomain, transportAddress)

            return wholeMsg

    else:  # v3arch

        def probeHashContext(self, snmpEngine):
            # this API is first introduced in pysnmp 4.2.6
            execCtx = snmpEngine.observer.getExecutionContext(
                'rfc3412.receiveMessage:request')

            (transportDomain,
             transportAddress,
             contextEngineId,
             contextName) = (
                execCtx['transportDomain'],
                execCtx['transportAddress'],
                execCtx['contextEngineId'],
                execCtx['contextName'].prettyPrint()
            )

            if contextEngineId == snmpEngine.snmpEngineID:
                contextEngineId = SELF_LABEL

            else:
                contextEngineId = contextEngineId.prettyPrint()

            for candidate in probeContext(
                    transportDomain, transportAddress,
                    contextEngineId, contextName):

                if len(candidate) > 32:
                    probedContextName = md5(candidate).hexdigest()

                else:
                    probedContextName = candidate

                try:
                    mibInstrum = self.snmpContext.getMibInstrum(probedContextName)

                except error.PySnmpError:
                    pass

                else:
                    log.info(
                        'Using %s selected by candidate %s; transport ID %s, '
                        'source address %s, context engine ID %s, '
                        'community name '
                        '"%s"' % (mibInstrum, candidate,
                                  univ.ObjectIdentifier(transportDomain),
                                  transportAddress[0], contextEngineId,
                                  probedContextName))
                    contextName = probedContextName
                    break
            else:
                mibInstrum = self.snmpContext.getMibInstrum(contextName)
                log.info(
                    'Using %s selected by contextName "%s", transport ID %s, '
                    'source address %s' % (mibInstrum, contextName,
                                           univ.ObjectIdentifier(transportDomain),
                                           transportAddress[0]))

            if not isinstance(mibInstrum, (MibInstrumController, DataIndexInstrumController)):
                log.error(
                    'LCD access denied (contextName does not match any data file)')
                raise NoDataNotification()

            return contextName

        class GetCommandResponder(cmdrsp.GetCommandResponder):

            def handleMgmtOperation(self, snmpEngine, stateReference, contextName, PDU, acInfo):
                try:
                    cmdrsp.GetCommandResponder.handleMgmtOperation(
                        self, snmpEngine, stateReference,
                        probeHashContext(self, snmpEngine),
                        PDU, (None, snmpEngine)  # custom acInfo
                    )

                except NoDataNotification:
                    self.releaseStateInformation(stateReference)

        class SetCommandResponder(cmdrsp.SetCommandResponder):

            def handleMgmtOperation(self, snmpEngine, stateReference, contextName, PDU, acInfo):
                try:
                    cmdrsp.SetCommandResponder.handleMgmtOperation(
                        self, snmpEngine, stateReference,
                        probeHashContext(self, snmpEngine),
                        PDU, (None, snmpEngine)  # custom acInfo
                    )

                except NoDataNotification:
                    self.releaseStateInformation(stateReference)

        class NextCommandResponder(cmdrsp.NextCommandResponder):

            def handleMgmtOperation(self, snmpEngine, stateReference, contextName, PDU, acInfo):
                try:
                    cmdrsp.NextCommandResponder.handleMgmtOperation(
                        self, snmpEngine, stateReference,
                        probeHashContext(self, snmpEngine),
                        PDU, (None, snmpEngine)  # custom acInfo
                    )

                except NoDataNotification:
                    self.releaseStateInformation(stateReference)

        class BulkCommandResponder(cmdrsp.BulkCommandResponder):

            def handleMgmtOperation(self, snmpEngine, stateReference, contextName, PDU, acInfo):
                try:
                    cmdrsp.BulkCommandResponder.handleMgmtOperation(
                        self, snmpEngine, stateReference,
                        probeHashContext(self, snmpEngine),
                        PDU, (None, snmpEngine)  # custom acInfo
                    )

                except NoDataNotification:
                    self.releaseStateInformation(stateReference)

    # Start configuring SNMP engine(s)

    transportDispatcher = AsyncoreDispatcher()

    if v2cArch:
        # Configure access to data index

        dataIndexInstrumController = DataIndexInstrumController()

        contexts = {univ.OctetString('index'): dataIndexInstrumController}

        with daemon.PrivilegesOf(procUser, procGroup):
            configureManagedObjects(confdir.data, dataIndexInstrumController)

        contexts['index'] = dataIndexInstrumController

        agentUDPv4Endpoints = []
        agentUDPv6Endpoints = []
        agentUnixEndpoints = []

        for opt in v3Args:
            if opt[0] == '--agent-udpv4-endpoint':
                agentUDPv4Endpoints.append(opt[1])

            elif opt[0] == '--agent-udpv6-endpoint':
                agentUDPv6Endpoints.append(opt[1])

            elif opt[0] == '--agent-unix-endpoint':
                agentUnixEndpoints.append(opt[1])

        if (not agentUDPv4Endpoints and
                not agentUDPv6Endpoints and
                not agentUnixEndpoints):
            log.error('agent endpoint address(es) not specified')
            return 1

        log.info('Maximum number of variable bindings in SNMP '
                 'response: %s' % maxVarBinds)

        # Configure socket server

        transportIndex = transportIdOffset
        for agentUDPv4Endpoint in agentUDPv4Endpoints:
            transportDomain = udp.domainName + (transportIndex,)
            transportIndex += 1

            transportDispatcher.registerTransport(
                transportDomain, agentUDPv4Endpoint[0])

            log.info('Listening at UDP/IPv4 endpoint %s, transport ID '
                     '%s' % (agentUDPv4Endpoint[1],
                             '.'.join([str(x) for x in transportDomain])))

        transportIndex = transportIdOffset

        for agentUDPv6Endpoint in agentUDPv6Endpoints:
            transportDomain = udp6.domainName + (transportIndex,)
            transportIndex += 1

            transportDispatcher.registerTransport(
                    transportDomain, agentUDPv6Endpoint[0])

            log.info('Listening at UDP/IPv6 endpoint %s, transport ID '
                     '%s' % (agentUDPv6Endpoint[1],
                             '.'.join([str(x) for x in transportDomain])))

        transportIndex = transportIdOffset

        for agentUnixEndpoint in agentUnixEndpoints:
            transportDomain = unix.domainName + (transportIndex,)
            transportIndex += 1

            transportDispatcher.registerTransport(
                    transportDomain, agentUnixEndpoint[0])

            log.info('Listening at UNIX domain socket endpoint %s, transport ID '
                     '%s' % (agentUnixEndpoint[1],
                             '.'.join([str(x) for x in transportDomain])))

        transportDispatcher.registerRecvCbFun(commandResponderCbFun)

    else:  # v3 mode

        if hasattr(transportDispatcher, 'registerRoutingCbFun'):
            transportDispatcher.registerRoutingCbFun(lambda td, t, d: td)

        else:
            log.info(
                'WARNING: upgrade pysnmp to 4.2.5 or later get multi-engine '
                'ID feature working!')

        if v3Args and v3Args[0][0] != '--v3-engine-id':
            v3Args.insert(0, ('--v3-engine-id', 'auto'))

        v3Args.append(('end-of-options', ''))

        def registerTransportDispatcher(snmpEngine, transportDispatcher,
                                        transportDomain):
            if hasattr(transportDispatcher, 'registerRoutingCbFun'):
                snmpEngine.registerTransportDispatcher(
                    transportDispatcher, transportDomain)

            else:
                try:
                    snmpEngine.registerTransportDispatcher(transportDispatcher)

                except error.PySnmpError:
                    log.msg('WARNING: upgrade pysnmp to 4.2.5 or later get '
                            'multi-engine ID feature working!')
                    raise

        snmpEngine = None

        transportIndex = {
            'udpv4': transportIdOffset,
            'udpv6': transportIdOffset,
            'unix': transportIdOffset
        }

        for opt in v3Args:

            if opt[0] in ('--v3-engine-id', 'end-of-options'):

                if snmpEngine:
                    log.info('--- SNMP Engine configuration')

                    log.info(
                        'SNMPv3 EngineID: '
                        '%s' % (hasattr(snmpEngine, 'snmpEngineID')
                                and snmpEngine.snmpEngineID.prettyPrint() or '<unknown>',))

                    if not v3ContextEngineIds:
                        v3ContextEngineIds.append((None, []))

                    log.msg.incIdent()

                    log.info('--- Data directories configuration')

                    for v3ContextEngineId, ctxDataDirs in v3ContextEngineIds:
                        snmpContext = context.SnmpContext(snmpEngine, v3ContextEngineId)
                        # unregister default context
                        snmpContext.unregisterContextName(null)

                        log.msg(
                            'SNMPv3 Context Engine ID: '
                            '%s' % snmpContext.contextEngineId.prettyPrint())

                        dataIndexInstrumController = DataIndexInstrumController()

                        with daemon.PrivilegesOf(procUser, procGroup):
                            configureManagedObjects(
                                ctxDataDirs or dataDirs or confdir.data,
                                dataIndexInstrumController,
                                snmpEngine,
                                snmpContext
                            )

                    # Configure access to data index

                    config.addV1System(snmpEngine, 'index',
                                       'index', contextName='index')

                    log.info('--- SNMPv3 USM configuration')

                    if not v3Users:
                        v3Users = ['simulator']
                        v3AuthKeys[v3Users[0]] = 'auctoritas'
                        v3AuthProtos[v3Users[0]] = 'MD5'
                        v3PrivKeys[v3Users[0]] = 'privatus'
                        v3PrivProtos[v3Users[0]] = 'DES'

                    for v3User in v3Users:
                        if v3User in v3AuthKeys:
                            if v3User not in v3AuthProtos:
                                v3AuthProtos[v3User] = 'MD5'

                        elif v3User in v3AuthProtos:
                            log.error(
                                'auth protocol configured without key for user '
                                '%s' % v3User)
                            return 1

                        else:
                            v3AuthKeys[v3User] = None
                            v3AuthProtos[v3User] = 'NONE'

                        if v3User in v3PrivKeys:
                            if v3User not in v3PrivProtos:
                                v3PrivProtos[v3User] = 'DES'

                        elif v3User in v3PrivProtos:
                            log.error(
                                'privacy protocol configured without key for user '
                                '%s' % v3User)
                            return 1

                        else:
                            v3PrivKeys[v3User] = None
                            v3PrivProtos[v3User] = 'NONE'

                        if (AUTH_PROTOCOLS[v3AuthProtos[v3User]] == config.usmNoAuthProtocol and
                                PRIV_PROTOCOLS[v3PrivProtos[v3User]] != config.usmNoPrivProtocol):
                            log.error(
                                'privacy impossible without authentication for USM user '
                                '%s' % v3User)
                            return 1

                        try:
                            config.addV3User(
                                snmpEngine,
                                v3User,
                                AUTH_PROTOCOLS[v3AuthProtos[v3User]],
                                v3AuthKeys[v3User],
                                PRIV_PROTOCOLS[v3PrivProtos[v3User]],
                                v3PrivKeys[v3User])

                        except error.PySnmpError as exc:
                            log.error(
                                'bad USM values for user %s: '
                                '%s' % (v3User, exc))
                            return 1

                        log.info('SNMPv3 USM SecurityName: %s' % v3User)

                        if AUTH_PROTOCOLS[v3AuthProtos[v3User]] != config.usmNoAuthProtocol:
                            log.info(
                                'SNMPv3 USM authentication key: %s, '
                                'authentication protocol: '
                                '%s' % (v3AuthKeys[v3User], v3AuthProtos[v3User]))

                        if PRIV_PROTOCOLS[v3PrivProtos[v3User]] != config.usmNoPrivProtocol:
                            log.info(
                                'SNMPv3 USM encryption (privacy) key: %s, '
                                'encryption protocol: '
                                '%s' % (v3PrivKeys[v3User], v3PrivProtos[v3User]))

                    snmpContext.registerContextName('index', dataIndexInstrumController)

                    log.info(
                        'Maximum number of variable bindings in SNMP response: '
                        '%s' % localMaxVarBinds)

                    log.info('--- Transport configuration')

                    if (not agentUDPv4Endpoints and
                            not agentUDPv6Endpoints and
                            not agentUnixEndpoints):
                        log.error(
                            'agent endpoint address(es) not specified for SNMP '
                            'engine ID %s' % v3EngineId)
                        return 1

                    for agentUDPv4Endpoint in agentUDPv4Endpoints:
                        transportDomain = udp.domainName + (transportIndex['udpv4'],)
                        transportIndex['udpv4'] += 1

                        registerTransportDispatcher(
                            snmpEngine, transportDispatcher, transportDomain)

                        config.addSocketTransport(
                            snmpEngine, transportDomain, agentUDPv4Endpoint[0])

                        log.info(
                            'Listening at UDP/IPv4 endpoint %s, transport ID '
                            '%s' % (agentUDPv4Endpoint[1],
                                    '.'.join([str(x) for x in transportDomain])))

                    for agentUDPv6Endpoint in agentUDPv6Endpoints:
                        transportDomain = udp6.domainName + (transportIndex['udpv6'],)
                        transportIndex['udpv6'] += 1

                        registerTransportDispatcher(
                            snmpEngine, transportDispatcher, transportDomain)

                        config.addSocketTransport(
                            snmpEngine,
                            transportDomain, agentUDPv6Endpoint[0])

                        log.info(
                            'Listening at UDP/IPv6 endpoint %s, transport ID '
                            '%s' % (agentUDPv6Endpoint[1],
                                    '.'.join([str(x) for x in transportDomain])))

                    for agentUnixEndpoint in agentUnixEndpoints:
                        transportDomain = unix.domainName + (transportIndex['unix'],)
                        transportIndex['unix'] += 1

                        registerTransportDispatcher(
                            snmpEngine, transportDispatcher, transportDomain)

                        config.addSocketTransport(
                            snmpEngine, transportDomain, agentUnixEndpoint[0])

                        log.info(
                            'Listening at UNIX domain socket endpoint '
                            '%s, transport ID '
                            '%s' % (agentUnixEndpoint[1], '.'.join(
                                [str(x) for x in transportDomain])))

                    # SNMP applications
                    GetCommandResponder(snmpEngine, snmpContext)
                    SetCommandResponder(snmpEngine, snmpContext)
                    NextCommandResponder(snmpEngine, snmpContext)
                    BulkCommandResponder(
                        snmpEngine, snmpContext).maxVarBinds = localMaxVarBinds

                    log.msg.decIdent()

                if opt[0] == 'end-of-options':
                    # Load up the rest of MIBs while running privileged
                    (snmpEngine
                     .msgAndPduDsp
                     .mibInstrumController
                     .mibBuilder.loadModules())
                    break

                # Prepare for next engine ID configuration

                v3ContextEngineIds = []
                dataDirs = []
                localMaxVarBinds = maxVarBinds
                v3Users = []
                v3AuthKeys = {}
                v3AuthProtos = {}
                v3PrivKeys = {}
                v3PrivProtos = {}
                agentUDPv4Endpoints = []
                agentUDPv6Endpoints = []
                agentUnixEndpoints = []

                try:
                    v3EngineId = opt[1]
                    if v3EngineId.lower() == 'auto':
                        snmpEngine = engine.SnmpEngine()

                    else:
                        snmpEngine = engine.SnmpEngine(
                            snmpEngineID=univ.OctetString(hexValue=v3EngineId))

                except Exception as exc:
                    log.error(
                        'SNMPv3 Engine initialization failed, EngineID "%s": '
                        '%s' % (v3EngineId, exc))
                    return 1

                config.addContext(snmpEngine, '')

            elif opt[0] == '--v3-context-engine-id':
                v3ContextEngineIds.append((univ.OctetString(hexValue=opt[1]), []))

            elif opt[0] == '--data-dir':
                if v3ContextEngineIds:
                    v3ContextEngineIds[-1][1].append(opt[1])

                else:
                    dataDirs.append(opt[1])

            elif opt[0] == '--max-varbinds':
                localMaxVarBinds = opt[1]

            elif opt[0] == '--v3-user':
                v3Users.append(opt[1])

            elif opt[0] == '--v3-auth-key':
                if not v3Users:
                    log.error('--v3-user should precede %s' % opt[0])
                    return 1

                if v3Users[-1] in v3AuthKeys:
                    log.error(
                        'repetitive %s option for user %s' % (opt[0], v3Users[-1]))
                    return 1

                v3AuthKeys[v3Users[-1]] = opt[1]

            elif opt[0] == '--v3-auth-proto':
                if opt[1].upper() not in AUTH_PROTOCOLS:
                    log.error('bad v3 auth protocol %s' % opt[1])
                    return 1

                else:
                    if not v3Users:
                        log.error('--v3-user should precede %s' % opt[0])
                        return 1

                    if v3Users[-1] in v3AuthProtos:
                        log.error(
                            'repetitive %s option for user %s' % (opt[0], v3Users[-1]))
                        return 1

                    v3AuthProtos[v3Users[-1]] = opt[1].upper()

            elif opt[0] == '--v3-priv-key':
                if not v3Users:
                    log.error('--v3-user should precede %s' % opt[0])
                    return 1

                if v3Users[-1] in v3PrivKeys:
                    log.error(
                        'repetitive %s option for user %s' % (opt[0], v3Users[-1]))
                    return 1

                v3PrivKeys[v3Users[-1]] = opt[1]

            elif opt[0] == '--v3-priv-proto':
                if opt[1].upper() not in PRIV_PROTOCOLS:
                    log.error('bad v3 privacy protocol %s' % opt[1])
                    return 1

                else:
                    if not v3Users:
                        log.error('--v3-user should precede %s' % opt[0])
                        return 1

                    if v3Users[-1] in v3PrivProtos:
                        log.error(
                            'repetitive %s option for user %s' % (opt[0], v3Users[-1]))
                        return 1

                    v3PrivProtos[v3Users[-1]] = opt[1].upper()

            elif opt[0] == '--agent-udpv4-endpoint':
                agentUDPv4Endpoints.append(opt[1])

            elif opt[0] == '--agent-udpv6-endpoint':
                agentUDPv6Endpoints.append(opt[1])

            elif opt[0] == '--agent-unix-endpoint':
                agentUnixEndpoints.append(opt[1])

    # Run mainloop

    transportDispatcher.jobStarted(1)  # server job would never finish

    with daemon.PrivilegesOf(procUser, procGroup, final=True):

        try:
            transportDispatcher.runDispatcher()

        except KeyboardInterrupt:
            log.info('Shutting down process...')

        finally:
            if variationModules:
                log.info('Shutting down variation modules:')

                for name, contexts in variationModules.items():
                    body = contexts[0]
                    try:
                        body['shutdown'](options=body['args'], mode='variation')

                    except Exception as exc:
                        log.error(
                            'Variation module "%s" shutdown FAILED: '
                            '%s' % (name, exc))

                    else:
                        log.info('Variation module "%s" shutdown OK' % name)

            transportDispatcher.closeDispatcher()

            log.info('Process terminated')

    return 0


if __name__ == '__main__':
    try:
        rc = main()

    except KeyboardInterrupt:
        sys.stderr.write('shutting down process...')
        rc = 0

    except Exception as exc:
        sys.stderr.write('process terminated: %s' % exc)

        for line in traceback.format_exception(*sys.exc_info()):
            sys.stderr.write(line.replace('\n', ';'))
        rc = 1

    sys.exit(rc)