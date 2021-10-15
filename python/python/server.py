import os
import time
import json
import codecs
import re
import psutil
from io import BytesIO
from flask import Flask, render_template, request, jsonify, send_from_directory, make_response, Response, send_file
from gevent import pywsgi, idle, spawn
from userConfig import setConfig, VERSION
from FIFOcache import Cache
from preset import preset, initPreset

config = {}
try:
  setConfig(config, VERSION)
  initPreset(config)
  dVer = {'version': config['version']}
except Exception as e:
  print(e)
staticMaxAge = 86400
app = Flask(__name__, root_path='.')
app.config['SERVER_NAME'] = '.'
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = staticMaxAge
startupTime = time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime())
def current():pass
current.session = None
current.path = None
current.key = None
current.eta = 0
current.setETA = True
current.fileSize = 0
E403 = ('Not authorized.', 403)
E404 = ('Not Found', 404)
OK = ('', 200)
cache = Cache(config['maxResultsKept'], OK, lambda *args: print('abandoned', *args))
busy = lambda: (jsonify(result='Busy', eta=current.eta), 503)
cwd = os.getcwd()
outDir = config['outDir']
uploadDir = config['uploadDir']
logPath = os.path.abspath(config['logPath'])
previewFormat = config['videoPreview']
downDir = os.path.join(app.root_path, outDir)
if not os.path.exists(outDir):
  os.mkdir(outDir)
with open('static/manifest.json') as manifest:
  assetMapping = json.load(manifest)
vendorsJs = assetMapping['vendors.js'] if 'vendors.js' in assetMapping else None
commonJs = assetMapping['common.js'] if 'common.js' in assetMapping else None
getKey = lambda session, request: request.values['path'] + str(session) if 'path' in request.values else current.key
toResponse = lambda obj, code=200: obj if type(obj) is tuple else (json.dumps(obj, ensure_ascii=False, separators=(',', ':')), code)

def pollNote():
  key = current.key
  while current.key:
    while current.key and not noter.poll():
      idle()
    res = None
    while noter.poll():
      res = noter.recv()
    if res and len(res):
      if current.setETA:
        updateETA(res)
      else:
        res.pop('total', 0)
        res.pop('gone', 0)
        res.pop('eta', 0)
      if 'fileSize' in res:
        current.fileSize = res['fileSize']
        del res['fileSize']
      if len(res):
        cache.update(key, res)

def acquireSession(request):
  if current.session:
    return busy()
  while noter.poll():
    noter.recv()
  current.session = request.values['session']
  current.path = request.values['path'] if 'path' in request.values else request.path
  current.key = current.path + str(current.session)
  spawn(pollNote)
  current.eta = 1
  updateETA(request.values)
  return False if current.session else E403

def controlPoint(path, fMatch, fUnmatch, fNoCurrent, check=lambda *_: True):
  def f():
    if not 'session' in request.values:
      return E403
    session = request.values['session']
    if not session:
      return E403
    if current.session:
      return spawn(fMatch, getKey(session, request)).get() if current.session == session and check(request) else fUnmatch()
    else:
      return fNoCurrent(session, request)
  app.route(path, methods=['GET', 'POST'], endpoint=path)(f)

def stopCurrent(*_):
  if current.session:
    if hasattr(current, 'root'):
      current.root.toStop() # pylint: disable=E1101
    current.stopFlag.set()
  return OK

def checkMsgMatch(request):
  if not 'path' in request.values:
    return True
  path = request.values['path']
  return path == current.path

def updateETA(res):
  if 'eta' in res:
    current.eta = res['eta']

def onConnect(key):
  while not (current.session is None or (key and cache.peek(key))):
    idle()
  res = None
  if key and cache.peek(key):
    res = cache.pop(key)
    return toResponse(res)
  else:
    return OK

def endSession(result):
  cache.put(current.key, result)
  current.key = None
  current.session = None
  return toResponse(result)

def makeHandler(name, prepare, final, methods=['POST']):
  def f():
    c = acquireSession(request)
    if c:
      return c
    try:
      args = prepare(request)
    except Exception as e:
      res = (str(e), 400)
      endSession(res)
      return res
    sender.send((name, *args))
    while not receiver.poll():
      idle()
    return endSession(final(receiver.recv(), request))
  app.route('/' + name, methods=methods, endpoint=name)(f)

def renderPage(item, header=None, footer=None):
  other = item[5] if len(item) > 5 else {}
  if vendorsJs:
    other['vendorsJs'] = vendorsJs
  if commonJs:
    other['commonJs'] = commonJs
  template = item[1]
  func = item[3]
  if func:
    g = lambda req: render_template(
      template, header=header, footer=footer, **other, **dict(zip(item[4], func(req))))
  else:
    with app.app_context():
      cache = render_template(template, header=header, footer=footer, **other)
    g = lambda _: cache
  def f():
    session = request.cookies.get('session')
    resp = make_response(g(request))
    t = time.time()
    if (not session) or (float(session) > t):
      resp.set_cookie('session', bytes(str(t), encoding='ascii'))
    if func:
      resp.cache_control.private = True
    else:
      resp.headers['Last-Modified'] = startupTime
      resp.cache_control.max_age = staticMaxAge
    return resp
  return f

ndoc = '<a href="{dirName}/{image}" class="w3effct-agile"><img src="{dirName}/{image}"'+\
  ' alt="" class="img-responsive" title="Solar Panels Image" />'+\
  '<div class="agile-figcap"><h4>相册</h4><p>图片{image}</p></div></a>'

def gallery(req):
  items = ()
  dirName = req.values['dir'] if 'dir' in req.values else outDir
  try:
    items = os.listdir(dirName)
  except:pass
  images = filter((lambda item:item.endswith('.png') or item.endswith('.jpg')), items)
  doc = []
  images = [*map(lambda image:ndoc.format(image=image, dirName=dirName), images)]
  for i in range((len(images) - 1) // 3 + 1):
    doc.append('<div class="col-sm-4 col-xs-4 w3gallery-grids">')
    doc.extend(images[i * 3:(i + 1) * 3])
    doc.append('</div>')
  return (''.join(doc),) if len(doc) else ('暂时没有图片，快去尝试放大吧',)

def getSystemInfo(info):
  import readgpu
  cuda, cudnn = readgpu.getCudaVersion()
  info.update({
    'cpu_count_phy': psutil.cpu_count(logical=False),
    'cpu_count_log': psutil.cpu_count(logical=True),
    'cpu_freq': psutil.cpu_freq().max,
    'disk_total': psutil.disk_usage(cwd).total // 2**20,
    'mem_total': psutil.virtual_memory().total // 2**20,
    'python': readgpu.getPythonVersion(),
    'torch': readgpu.getTorchVersion(),
    'cuda': cuda,
    'cudnn': cudnn,
    'gpus': readgpu.getGPUProperties()
  })
  readgpu.uninstall()
  del readgpu
  return info

def getDynamicInfo(_):
  disk_free = psutil.disk_usage(cwd).free // 2**20
  mem_free = psutil.virtual_memory().free // 2**20
  return disk_free, mem_free, current.session, current.path

def setOutputName(args, fp):
  if not len(args):
    args = ({'op': 'output'},)
  if 'file' in args[-1]:
    return args
  base, ext = os.path.splitext(fp.filename)
  path = '{}/{}{}'.format(outDir, base, ext)
  i = 0
  while os.path.exists(path):
    i += 1
    path = '{}/{}_{}{}'.format(outDir, base, i, ext)
  args[-1]['file'] = path
  return args

def responseEnhance(t, req):
  res, code = t
  if 'eta' in req.values:
    res['eta'] = float(req.values['eta'])
  res.update((k, int(req.values[k])) for k in ('gone', 'total') if k in req.values)
  return toResponse(res, code)

about_updater = lambda *_: [codecs.open('./update_log.txt', encoding='utf-8').read()]

header = codecs.open('./templates/1-header.html','r','utf-8').read()
footer = codecs.open('./templates/1-footer.html','r','utf-8').read()
routes = [
  #(query path, template file, active page name, request handler, request result names, dict of static variables)
  ('/', 'index.html', '主页', None, None, dVer),
  ('/video', 'video.html', 'AI视频', None, None, dVer),
  ('/batch', 'batch.html', '批量放大', None, None, dVer),
  ('/document', 'document.html', None, None, None, dVer),
  ('/about', 'about.html', None, about_updater, ['log'], dVer),
  ('/system', 'system.html', None, getDynamicInfo, ['disk_free', 'mem_free', 'session', 'path'], getSystemInfo(dVer)),
  ('/gallery', 'gallery.html', None, gallery, ['var'], dVer),
  ('/lock', 'lock.html', None, None, None, dVer)
]

for item in routes:
  if item[2]:
    pattern = '>' + item[2]
    new = 'class=\"active\"' + pattern
    h = re.sub(pattern,new,header)
  else:
    h = header
  app.route(item[0], endpoint=item[0])(renderPage(item, h, footer))

identity = lambda x, *_: x
readOpt = lambda req: json.loads(req.values['steps'])
onRequestCache = lambda session, request: cache.pop(getKey(session, request))
controlPoint('/stop', stopCurrent, lambda: E403, lambda *_: E404)
controlPoint('/msg', onConnect, busy, onRequestCache, checkMsgMatch)
app.route('/log', endpoint='log')(lambda: send_file(logPath, add_etags=False))
app.route('/favicon.ico', endpoint='favicon')(lambda: send_from_directory(app.root_path, 'logo3.ico'))
app.route("/{}/.preview.{}".format(outDir, previewFormat), endpoint="preview")(
  lambda: Response(current.getPreview(), mimetype="image/{}".format(previewFormat)))
sendFromDownDir = lambda filename: send_from_directory(downDir, filename)
app.route("/{}/<path:filename>".format(outDir), endpoint='download')(sendFromDownDir)
lockFinal = lambda result, *_: (jsonify(result='Interrupted', remain=result), 200) if result > 0 else (jsonify(result='Idle'), 200)
makeHandler('lockInterface', (lambda req: [int(float(readOpt(req)[0]['duration']))]), lockFinal, ['GET', 'POST'])
makeHandler('systemInfo', (lambda _: []), identity, ['GET', 'POST'])
getReqFile = lambda f: lambda req: f(req, req.files['file'])
imageEnhancePrep = lambda req, fp: (current.writeFile(fp), *setOutputName(readOpt(req), fp))
makeHandler('image_enhance', getReqFile(imageEnhancePrep), responseEnhance)
app.route('/preset', methods=['GET', 'POST'], endpoint='preset')(preset)

def videoEnhancePrep(req):
  if not os.path.exists(uploadDir):
    os.mkdir(uploadDir)
  for k in ('url', 'cmd'):
    v = req.values.get(k, None)
    if v:
      return (v, k, *readOpt(req))
  vidfile = req.files['file']
  path ='{}/{}'.format(uploadDir, vidfile.filename)
  vidfile.save(path)
  return (path, False, *setOutputName(readOpt(req), vidfile))
makeHandler('video_enhance', videoEnhancePrep, responseEnhance)

@app.route('/batch_enhance', methods=['POST'])
def batchEnhance():
  c = acquireSession(request)
  if c:
    return c
  current.stopFlag.clear()
  count = 0
  fail = 0
  fails = []
  done = []
  result = 'Success'
  fileList = request.files.getlist('file')
  output_path = '{}/{}/'.format(outDir, int(time.time()))
  if not os.path.exists(output_path):
    os.makedirs(output_path)
  opt = readOpt(request)
  total = len(fileList)
  print('batch total: {}'.format(total))
  opt.append(dict(trace=False, op='output'))
  current.setETA = False
  for image in fileList:
    if current.stopFlag.is_set():
      result = 'Interrupted'
      break
    name = os.path.join(output_path, image.filename)
    start = time.time()
    opt[-1]['file'] = name
    current.fileSize = current.writeFile(image)
    sender.send(('batch', current.fileSize, *opt))
    while not receiver.poll():
      idle()
    output = receiver.recv()
    count += 1
    note = {
      'eta': (total - count) * (time.time() - start),
      'gone': count,
      'total': total
    }
    updateETA(note)
    if output[1] == 200:
      note['preview'] = name
      done.append(name)
    else:
      fail += 1
      fails.append(name)
    cache.put(current.key, note)
  current.setETA = True
  return endSession({'result': (result, count, done, fail, fails, output_path)})

def runserver(taskInSender, taskOutReceiver, noteReceiver, stopEvent, mm, isWindows):
  global sender, receiver, noter
  sender = taskInSender
  receiver = taskOutReceiver
  noter = noteReceiver
  current.stopFlag = stopEvent
  mmView = memoryview(mm) if isWindows else mm.buf
  current.getPreview = lambda: BytesIO(mmView[:current.fileSize])
  if not isWindows:
    mm = mm.buf.obj
  def writeFile(file):
    mm.seek(0)
    return file._file.readinto(mm)
  current.writeFile = writeFile
  def f(host, port):
    app.debug = False
    app.config['SERVER_NAME'] = None
    server = pywsgi.WSGIServer((host, port), app, environ={'SERVER_NAME': ''})
    print('Current working directory: {}'.format(cwd))
    print('Server starts to listen on http://{}:{}/, press Ctrl+C to exit.'.format(host, port))
    server.serve_forever()
  return f