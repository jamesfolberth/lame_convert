"""
A simple script to walk a directory, call the system `lame` util on each MP3,
and copy the converted file to a clone directory.  It looks like `lame` uses
a single thread, so we'll use `multiprocessing` to run transcoding in parallel.
"""

import os, shutil, string, re, time
import subprocess
import multiprocessing as mp
import multiprocessing.queues # to subclass mp.Queue()
import queue
import curses
import argparse

# for debug/dev only
from pprint import pprint
  
SENTINEL=None
LAME_EXT=frozenset(['mp3', 'wav']) #TODO JMF 2017/04/29: what other types does lame handle?
IMAGE_EXT=frozenset(['jpg', 'png'])

class _StateQueue(mp.queues.Queue):
  """
  A `put` to this queue will clear it and put a single item.
  A `get` to this queue will get a single item.
  """
  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)

    self._state_lock = mp.Lock() # use our own lock so we don't goof up the
                                 # base queue's sync objects

  def put(self, *args, **kwargs):
    with self._state_lock:
      #keep = [] # don't delete these items (bugfix race condition)
      while not self.empty():
        try:
          super().get(False)
          #item = super().get(False)
          #if 'msg' in item and item['msg'].get('op', '') == 'transcode_done':
          #  print('keeping\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n')
          #  keep.append(item)
        except:
          pass
      #for item in keep:
      #  super().put(*args, **kwargs)
      super().put(*args, **kwargs)
  
  def get(self, *args, **kwargs):
    with self._state_lock:
      return super().get(*args, **kwargs)

def StateQueue(maxsize=0):
  return _StateQueue(maxsize, ctx=mp.get_context())


class ConverterProducer(mp.Process):
  def __init__(self, args, files_q, info_qs=[]):
    super().__init__()
    
    self.args = args

    self.indir = args.indir
    self.outdir = args.outdir
    self.aindir = os.path.abspath(self.indir)
    self.aoutdir = os.path.abspath(self.outdir)

    self.files_q = files_q
    self.files_q_timeout = 0.1 # seconds

    self.info_qs = info_qs

    self.worker_states = {}

    self.checkArgs()

  def checkArgs(self):
    
    if not os.path.isdir(self.aindir):
      raise ValueError('The input directory does not exist.')

    if not os.path.isdir(self.aoutdir):
      os.makedirs(self.aoutdir)

    if os.path.samefile(self.aindir, self.aoutdir):
      #TODO JMF 2017/04/23: allow overwriting indir's files, or name `outdir`?
      raise ValueError('The input and output directory cannot be the '
          'same (for now).')

    self.do_curses = not (self.args.verbose or self.args.dry_run)

  def filenames(self):
    for dirpath, dirnames, filenames in os.walk(self.indir):
      if filenames: # only care if files exist in dir; don't care about dirnames
        relpath = os.path.relpath(dirpath, self.indir)
                
        # absolute paths
        #infilenames = list(map(lambda fn: os.path.join(self.aindir, relpath, fn), filenames))
        #outfilenames = list(map(lambda fn: os.path.join(self.aoutdir, relpath, fn), filenames))
        
        # relative paths
        infilenames = list(map(lambda fn: os.path.join(self.indir, relpath, fn), filenames))
        outfilenames = list(map(lambda fn: os.path.join(self.outdir, relpath, fn), filenames))
        
        yield {'newpath': os.path.join(self.aoutdir, relpath),
            'infilenames': infilenames, 'outfilenames': outfilenames}
    
    for _ in range(self.args.num_workers):
      yield SENTINEL

  def update_worker_states(self):
    # Try to get info from the workers' info queues
    for info_q in self.info_qs:
      try:
        info_item = info_q.get(False)
        if info_item is SENTINEL:
          continue
        else:
          self.worker_states[info_item['pid']] = info_item
      except queue.Empty:
        pass
  
  def handle_states(self):
    if not (self.args.verbose or self.args.dry_run):
      msgs = []
      num_done = 0
      for worker, state in self.worker_states.items():
        num_done += state.get('transcodes_done', 0)
        finished = state.get('finished', False)

        text = ''
        msg = state.get('msg', {})
        op = msg.get('op', '')
        if op == 'mkdir':
          text = 'mkdir:\n  -->   {}'.format(msg['newpath'])

        elif op == 'rm':
          text = 'removing failed file:\n  -->  {}'.format(msg['file'])

        elif op == 'copy':
          max_len = max(len(msg['infile']), len(msg['outfile'])) # to right align
          text = 'copy:\n      {1:>{0}}\n  -->  {2:>{0}}'.format(
              max_len, msg['infile'], msg['outfile'])

        elif op == 'transcode':
          max_len = max(len(msg['infile']), len(msg['outfile'])) # to right align
          text = 'transcode:\n       {1:>{0}}\n  -->  {2:>{0}}'.format(
              max_len, msg['infile'], msg['outfile'])
 
          if 'hist' in msg:
            text += '\n'+msg['hist']
  
        if not finished:
          msgs.append((worker, text))
      
      self.num_done = num_done
      msgs.sort(key=lambda t: t[0]) # sort by PID
      
      text = 'Percent complete: {2:3.1f}%  ({0:{3}d} of {1:{3}d})\n'.format(
          self.num_done, self.num_todo, 100.*self.num_done/self.num_todo,
          len(str(self.num_todo)))
      text += '-'*len(text)+'\n'
      for i, msg in enumerate(msgs):
        text += 'Worker {0:4d}:\n{1}'.format(*msg)
        if i < len(msgs)-1: text += '\n\n'


      # this isn't robust, and probably isn't right
      lines = text.splitlines()
      pad_w = max(map(lambda l: len(l), lines))
      pad_h = len(lines)

      def refresh():
        try:
          self.pad.move(0,0) # always put the cursor at (0,0)
          self.pad.refresh(self.row,self.col, 0,0, self.win_h-1,self.win_w-1)
        except curses.error as e:
          pass
        
      # display with curses
      try:
        if pad_h <= self.pad_h or pad_w <= self.pad_w: self.win.clrtobot()

        self.pad.erase()
        #TODO this creaashes on X window resize
        self.pad.resize(max(pad_h,self.win_h), max(pad_w,self.win_w))
        self.pad_h, self.pad_w = pad_h, pad_w

        self.pad.addstr(text)
      except curses.error:
        pass
      
      refresh()

      # move with curses
      ch = self.pad.getch()
      while ch != -1:
        if (ch == curses.KEY_DOWN or ch == ord('j')) and self.row < pad_h - self.win_h:
          self.row += 1

        elif (ch == curses.KEY_UP or ch == ord('k')) and self.row > 0:
          self.row -= 1

        elif (ch == curses.KEY_RIGHT or ch == ord('l')) and self.col < pad_w - self.win_w:
          self.col += 1

        elif (ch == curses.KEY_LEFT or ch == ord('h')) and self.col > 0:
          self.col -= 1

        elif ch == curses.KEY_NPAGE:
          self.row = min(self.row+self.win_h, pad_h-self.win_h)

        elif ch == curses.KEY_PPAGE:
          self.row = max(self.row-self.win_h, 0)

        elif ch == curses.KEY_HOME:
          self.row = 0

        elif ch == curses.KEY_END:
          self.row = pad_h-self.win_h
        
        self.pad.redrawwin()
        refresh()

        ch = self.pad.getch()
  
  def init_curses(self):
    # curses stuff adapted from
    # https://github.com/python/cpython/blob/2.7/Demo/curses/repeat.py
    # http://stackoverflow.com/a/18295415
    self.win = curses.initscr()
    curses.noecho()
    self.win.keypad(True)
    curses.cbreak()

    self.win_h, self.win_w = self.win.getmaxyx()
    self.pad = curses.newpad(self.win_h,self.win_w)
    self.pad_h, self.pad_w = self.win_h, self.win_w
    self.pad.timeout(10) # milliseconds
    self.row = 0; self.col = 0;
    self.pad.scrollok(True)
    self.pad.keypad(True)

  def finish_curses(self):
    curses.nocbreak()
    self.pad.keypad(False)
    curses.echo()
    curses.endwin()

  def run(self):
    self.num_done = 0
    self.num_todo = sum(map(lambda fns:\
        sum((os.path.splitext(fn)[1].lower()[1:] in LAME_EXT) for fn in fns['infilenames'])\
        if fns is not SENTINEL else 0,
        self.filenames()))

    if self.do_curses: self.init_curses()

    try:
      # main loop
      for filenames in self.filenames():
        put_succeeded = False
        while not put_succeeded:
          # Try to put an item on the file queue, but don't block too long
          try: 
            self.files_q.put(filenames, True, self.files_q_timeout)
            put_succeeded = True
          except queue.Full as e: # we didn't put anything on the queue
            put_succeeded = False
          
          if self.do_curses:
            self.update_worker_states()
            self.handle_states() 
      
      # if main loop finished normally, keep printing info until all workers are done
      else:
        while not all(map(lambda s: s['finished'], self.worker_states.values())):
          self.update_worker_states()
          self.handle_states()
          time.sleep(self.args.disptime)

    finally:
      if self.do_curses: self.finish_curses()


class ConverterConsumer(mp.Process):
  def __init__(self, args, files_q, info_q=None):
    super().__init__()
    
    self.args = args
    self.files_q = files_q
    self.info_q = info_q
    
    # extension to use when we're still working on the output file
    self.extension = '.wrk'

    self.transcodes_done = 0
    self.finished = False
    
    # From http://stackoverflow.com/a/38662876  
    self.ansi_escape = re.compile(r'(\x9B|\x1B\[)[0-?]*[ -\/]*[@-~]')

    self.lame_header = re.compile(r'^\s*Frame\s*\|\s*CPU time/estim\s*\|\s*REAL '
        'time/estim\s*\|\s*play/CPU\s*\|\s*ETA')
    self.kbps_footer = re.compile(r'^\s*kbps\s*LR\s*MS\*\%')

  def read_proc_stdout(self, proc, inf, outf):
    
    # parse out (and remove) the lame header
    header = ''
    while True:
      line = proc.stdout.readline().decode()
      line = self.ansi_escape.sub('', line)
      
      if not line:
        break

      if self.lame_header.match(line):
        break
      header += line

    lines = [line]
    
    # parse out what lame is repeatedly printing.
    # this is a bit of a hack
    for line in proc.stdout:
      line = self.ansi_escape.sub('', line.decode())
      
      # The 'last' line contains the bitrate and other info \r frame, percentage, timing, info
      #TODO JMF 2017/04/29: this might break.  Make a regex for that type of line
      #     view it with print(repr(line)) to see what's under the hood
      if '\r' in line:
        last_line, begin_line = line.split('\r')
        lines.append(last_line)
        #hist = ''.join(lines)
        hist = header + ''.join(lines)
        
        lines.clear()
        lines.append(begin_line)

        self.send_state_msg({'op': 'transcode',
                             'infile': inf,
                             'outfile': outf,
                             'hist': hist
                             })

      else:
        lines.append(line)
      
    proc.wait()
  
  def send_state_msg(self, msg={}):
    self.info_q.put({'pid': self.pid, 
                     'transcodes_done': self.transcodes_done,
                     'finished': self.finished,
                     'msg': msg
                     })
  
  def run(self):
    while True:
      try:

        item = self.files_q.get(block=True)
        if item is SENTINEL:
          self.finished = True
          self.send_state_msg()
          return 

        if 'newpath' in item and 'infilenames' in item and 'outfilenames' in item:
          newpath = item['newpath']
          infilenames = item['infilenames']
          outfilenames = item['outfilenames']
          
          # make output dir if necessary
          if not os.path.isdir(newpath):
            msg = 'mkdir:\n  -->   {}'.format(newpath)
            if self.args.dry_run: print(msg)
            else:
              if not self.args.clean:
                if self.args.verbose: print(msg)
                self.send_state_msg({'op': 'mkdir',
                                     'newpath': newpath
                                    })
                os.makedirs(newpath)
          
          # loop over files
          for inf, outf in zip(infilenames, outfilenames):
            # skip if outfile already exists
            if os.path.isfile(outf): 
              if os.path.splitext(outf)[1].lower()[1:] in LAME_EXT:
                self.transcodes_done += 1 # for display only
                self.send_state_msg()
              continue
            
            # we failed processing this one earlier; try again
            if os.path.isfile(outf+self.extension):
              msg = 'removing failed file:\n  -->  {}'.format(outf+self.extension)
              if self.args.clean or self.args.verbose or self.args.dry_run: 
                print(msg)
              if self.args.clean or not self.args.dry_run:
                self.send_state_msg({'op': 'rm',
                                     'file': outf+self.extension
                                     })
                os.unlink(outf+self.extension)
            
            # do work: transcode mp3; copy jpg and png
            max_len = max(len(inf), len(outf)) # to right align
            base_msg = ':\n       {1:>{0}}\n  -->  {2:>{0}}'.format(max_len, inf, outf)
            if self.args.clean: continue
            else:
              outf_wrk = outf+self.extension
              ext = os.path.splitext(outf)[1]

              if ext.lower()[1:] in LAME_EXT:
                if self.args.verbose or self.args.dry_run: print('transcode'+base_msg)
                if self.args.dry_run: continue
                self.send_state_msg({'op': 'transcode',
                                     'infile': inf,
                                     'outfile': outf,
                                     })

                #TODO JMF 2017/04/25: if bitrate is less than target average bitrate, then 
                #                     don't transcode.
                #                     See the `mediainfo` package
                
                #TODO JMF 2017/04/23: lame stuff
                #TODO JMF 2017/04/23: what's the best function to use here?
                #subprocess.call(['lame', '--quiet', '--abr', '160', '-b', '96', inf, outf_wrk])
                lame_args = ['lame']
                lame_args.extend(self.args.lame_args.split())
                lame_args.extend(('--disptime', str(self.args.disptime)))
                lame_args.extend((inf, outf_wrk))
 
                #subprocess.call(lame_args)
                proc = subprocess.Popen(lame_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

                self.read_proc_stdout(proc, inf, outf)
                
                self.transcodes_done += 1
                self.send_state_msg()
                
              elif ext.lower()[1:] in IMAGE_EXT:
                if self.args.verbose or self.args.dry_run: print('copy'+base_msg)
                if self.args.dry_run: continue
                self.send_state_msg({'op': 'copy',
                                     'infile': inf,
                                     'outfile': outf
                                     })

                shutil.copy2(inf, outf_wrk)
              
              else:
                continue # unrecognized file, so do nothing
              
              # if the hard part (transcode/copy) was a success, remove .wrk extension
              os.rename(outf_wrk, outf)
      
      except Exception as e:
        print(e)


#TODO JMF 2017/04/23: <Ctrl-C> grabber, so we can print warning to user then die?

def main(args):
  # initialize
  files_q = mp.Queue(args.queue_size)

  if args.dry_run: args.num_workers = 1 # want predictable output

  consumers = []
  info_qs = [] 
  for _ in range(args.num_workers):
    info_q = StateQueue(args.queue_size)
    info_qs.append(info_q)
    consumers.append(ConverterConsumer(args, files_q, info_q=info_q))
  
  producer = ConverterProducer(args, files_q, info_qs=info_qs)
  
  # start up processes
  producer.start()
  for consumer in consumers:
    consumer.start()
  
  # wait to finish
  for consumer in consumers:
    consumer.join()


if __name__ == '__main__':
  parser = argparse.ArgumentParser(
      description='Convert MP3 files from a directory tree to use average/'
          'variable bitrate and copy the files to a cloned directory.')

  # indir/outdir
  parser.add_argument('indir', type=str,
                    help='The directory of original MP3 files.')
  parser.add_argument('outdir', type=str,
                    help='The directory of output MP3 files.')
  
  # multiprocessing args
  parser.add_argument('--queue-size', type=int, default=2*mp.cpu_count(),
      help='The maximum number of items on the queue.')
  parser.add_argument('--num-workers', type=int, default=mp.cpu_count(),
      help='The number of worker processes to run simultaneously.')
  
  # util args
  parser.add_argument('--clean', action='store_true',
      help='Clean up any "work" files that are left over from failed processing.')
  parser.add_argument('--dry-run', action='store_true',
      help='Do a dry run of the processing, printing files to be converted')
  parser.add_argument('--verbose', action='store_true',
      help='Be verbose in the processing')


  #TODO JMF 2017/04/23: lame parameters here, with sane defaults
  #parser.add_argument('--lame-args', type=str, default='--abr 160 -b 96',
  parser.add_argument('--lame-args', type=str, default='--preset medium',
      help='The optional arguments pased to `lame`.')
  parser.add_argument('--disptime', type=float, default=0.1,
      help='The time between screen updates, which also overrides the `--disptime`'
           ' argument passed in --lame-args for `lame`.')


  args = parser.parse_args()

  main(args)

# vim: set sw=2 sts=2 ts=4:
