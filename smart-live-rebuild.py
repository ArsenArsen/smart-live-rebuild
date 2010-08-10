#!/usr/bin/python
#	vim:fileencoding=utf-8
# Check all live ebuilds for updates and rebuild them if necessary.
# (C) 2010 Michał Górny <gentoo@mgorny.alt.pl>
# Released under the terms of the 3-clause BSD license or the GPL-2 license.

PV = '0.5.1'

import bz2, os, pickle, re, shutil, signal, subprocess, sys, tempfile, time
import portage

from optparse import OptionParser
try:
	from configparser import ConfigParser, NoOptionError
except ImportError: # py2
	from ConfigParser import ConfigParser, NoOptionError

class out:
	red = '\033[1;31m'
	green = '\033[32m'
	lime = '\033[1;32m'
	yellow = '\033[1;33m'
	cyan = '\033[36m'
	turq = '\033[1;36m'
	white = '\033[1;37m'
	reset = '\033[0m'

	s1reset = lime
	s2reset = green
	s3reset = cyan
	errreset = yellow

	@classmethod
	def monochromize(self):
		for k in dir(self):
			if not k.startswith('_'):
				v = getattr(self, k)
				if isinstance(v, str) and v.startswith('\033'):
					setattr(self, k, '')

	@classmethod
	def s1(self, msg):
		self.out('%s*** %s%s\n' % (self.s1reset, msg, self.reset))
	@classmethod
	def s2(self, msg):
		self.out('%s->%s  %s\n' % (self.s2reset, self.reset, msg))
	@classmethod
	def s3(self, msg):
		self.out('%s-->%s %s\n' % (self.s3reset, self.reset, msg))

	@classmethod
	def err(self, msg):
		self.out('%s!!!%s %s%s%s\n' % (self.red, self.reset, self.errreset, msg, self.reset))

	@staticmethod
	def out(msg):
		sys.stderr.write(msg)

class Shared:
	envtmpf = None
	opts = None

	@classmethod
	def opentmp(self):
		self.envtmpf = tempfile.NamedTemporaryFile('w+b')

	@classmethod
	def closetmp(self):
		self.envtmpf.close()

class NonLiveEbuild(Exception):
	pass

class VCSSupport:
	inherit = None
	reqenv = []
	optenv = []

	@classmethod
	def match(self, inherits):
		if self.inherit is None:
			raise NotImplementedError('VCS class needs to either override inherit or match()')
		return self.inherit in inherits

	def bashparse(self, envf, vars):
		f = Shared.envtmpf
		f.seek(0, 0)
		f.truncate(0)
		shutil.copyfileobj(envf, f)
		f.flush()

		script = 'source "%s"||exit 1;%s' % (f.name,
			';echo -ne "\\0";'.join(['echo -n "${%s}"' % x for x in vars]))

		return dict(zip(vars, self.call(['bash', '-c', script]).split('\0')))

	def __init__(self, cpv, envf):
		self.cpv = [cpv]
		self.env = self.bashparse(envf, self.reqenv + self.optenv)

		missingvars = [v for v in self.reqenv if self.env[v] == '']
		if len(missingvars) > 0:
			raise KeyError('Environment does not declare: %s' % self.reqenv)

	def getpath(self):
		raise NotImplementedError('VCS class needs to override getpath()')

	def append(self, vcs):
		if not isinstance(vcs, self.__class__):
			raise ValueError('Unable to append %s to %s' % (vcs.__class__, self.__class__))
		self.cpv.append(vcs.cpv[0])

	def getsavedrev(self):
		return None

	def getrev(self):
		raise NotImplementedError('VCS class needs to override getrev() or update()')

	@staticmethod
	def revcmp(oldrev, newrev):
		return oldrev == newrev

	@staticmethod
	def call(cmd):
		return subprocess.Popen(cmd, stdout=subprocess.PIPE).communicate()[0].decode('utf8')

	def getupdatecmd(self):
		raise NotImplementedError('VCS class needs to override getupdatecmd()')

	def diffstat(self, oldrev, newrev):
		pass

	def startupdate(self):
		out.s2(str(self))
		os.chdir(self.getpath())
		self.oldrev = (not Shared.opts.local_rev and self.getsavedrev()) or self.getrev()

		if Shared.opts.network:
			cmd = self.getupdatecmd()
			out.s3(cmd)
			self.subprocess = subprocess.Popen(cmd, stdout=sys.stderr, shell=True)
		else:
			self.subprocess = None

		return self.subprocess

	def endupdate(self, blocking = False):
		if self.subprocess is None:
			ret = 0
		elif blocking:
			ret = self.subprocess.wait()
		else:
			ret = self.subprocess.poll()
			if ret is None:
				return None

		if ret == 0:
			os.chdir(self.getpath())
			newrev = self.getrev()
			if Shared.opts.jobs > 1:
				out.s2(str(self))

			if self.revcmp(self.oldrev, newrev):
				out.s3('at rev %s%s%s (no changes)' % (out.green, self.oldrev, out.reset))
				return False
			else:
				self.diffstat(self.oldrev, newrev)
				out.s3('update from %s%s%s to %s%s%s' % (out.green, self.oldrev, out.reset, out.lime, newrev, out.reset))
				return True
		else:
			raise Exception('update command returned non-zero result')

	def abortupdate(self):
		if self.subprocess is not None:
			self.subprocess.terminate()

	def __str__(self):
		return self.cpv

class GitSupport(VCSSupport):
	inherit = 'git'
	reqenv = ['EGIT_BRANCH', 'EGIT_PROJECT', 'EGIT_STORE_DIR', 'EGIT_UPDATE_CMD']
	optenv = ['EGIT_COMMIT', 'EGIT_DIFFSTAT_CMD', 'EGIT_HAS_SUBMODULES', 'EGIT_OPTIONS', 'EGIT_REPO_URI', 'EGIT_VERSION']

	def __init__(self, cpv, env):
		VCSSupport.__init__(self, cpv, env)
		if self.env['EGIT_COMMIT'] and self.env['EGIT_COMMIT'] != self.env['EGIT_BRANCH']:
			raise NonLiveEbuild('EGIT_COMMIT set, package is not really a live one')

	def getpath(self):
		return '%s/%s' % (self.env['EGIT_STORE_DIR'], self.env['EGIT_PROJECT'])

	def __str__(self):
		return self.env['EGIT_REPO_URI'] or self.cpv

	def getsavedrev(self):
		return self.env['EGIT_VERSION']

	def getrev(self):
		branch = self.env['EGIT_BRANCH']
		if self.env['EGIT_HAS_SUBMODULES']:
			branch = 'origin/%s' % branch
		return self.call(['git', 'rev-parse', branch]).split('\n')[0]

	def getupdatecmd(self):
		if self.env['EGIT_HAS_SUBMODULES']:
			return '%s %s' % (self.env['EGIT_UPDATE_CMD'], self.env['EGIT_OPTIONS'])
		else:
			return '%s %s origin %s:%s' % (self.env['EGIT_UPDATE_CMD'], self.env['EGIT_OPTIONS'], self.env['EGIT_BRANCH'], self.env['EGIT_BRANCH'])

	def diffstat(self, oldrev, newrev):
		subprocess.Popen('%s %s..%s' % (self.env['EGIT_DIFFSTAT_CMD'] or 'git diff', oldrev, newrev), stdout=sys.stderr, shell=True).wait()

class HgSupport(VCSSupport):
	inherit = 'mercurial'
	reqenv = ['EHG_PROJECT', 'EHG_PULL_CMD', 'EHG_REPO_URI']
	optenv = ['EHG_REVISION']

	trustopt = ['--config', 'trusted.users=portage']

	def __init__(self, cpv, env):
		VCSSupport.__init__(self, cpv, env)
		if self.env['EHG_REVISION'] and self.env['EHG_REVISION'] != 'tip':
			raise NonLiveEbuild('EHG_REVISION set, package is not really a live one')

	def getpath(self):
		dd = portage.settings['PORTAGE_ACTUAL_DISTDIR'] or portage.settings['DISTDIR']
		bn = os.path.basename(self.env['EHG_REPO_URI']) or os.path.basename(os.path.dirname(self.env['EHG_REPO_URI']))
		assert (bn != '')

		return '%s/hg-src/%s/%s' % (dd, self.env['EHG_PROJECT'], bn)

	def __str__(self):
		return self.env['EHG_REPO_URI'] or self.cpv

	def getrev(self):
		return self.call(['hg', 'tip', '--template', '{node}'] + self.trustopt)

	def getupdatecmd(self):
		return ' '.join([self.env['EHG_PULL_CMD']] + self.trustopt)

	def diffstat(self, oldrev, newrev):
		subprocess.Popen(['hg', 'diff', '--stat', '-r', oldrev, '-r', newrev] + self.trustopt, stdout=sys.stderr).wait()

class SvnSupport(VCSSupport):
	inherit = 'subversion'
	reqenv = ['ESVN_STORE_DIR', 'ESVN_UPDATE_CMD', 'ESVN_WC_PATH']
	optenv = ['ESVN_REVISION', 'ESVN_OPTIONS', 'ESVN_PASSWORD', 'ESVN_REPO_URI', 'ESVN_USER', 'ESVN_WC_REVISION']

	revre = re.compile('(?m)^Last Changed Rev: (\d+)$')

	def __init__(self, cpv, env):
		VCSSupport.__init__(self, cpv, env)
		if self.env['ESVN_REPO_URI'] and self.env['ESVN_REPO_URI'].find('@') != -1:
			raise NonLiveEbuild('ESVN_REPO_URI specifies revision, package is not really a live one')
		elif self.env['ESVN_REVISION']:
			raise NonLiveEbuild('ESVN_REVISION set, package is not really a live one')

	def getpath(self):
		return self.env['ESVN_WC_PATH']

	def __str__(self):
		return self.env['ESVN_REPO_URI'] or self.cpv

	def getsavedrev(self):
		return self.env['ESVN_WC_REVISION']

	def getrev(self):
		svninfo = self.call(['svn', 'info'])
		m = self.revre.search(svninfo)
		return m.group(1) if m is not None else None

	@staticmethod
	def revcmp(oldrev, newrev):
		return oldrev >= newrev

	def getupdatecmd(self):
		cmd = '%s %s --config-dir %s/.subversion' % (self.env['ESVN_UPDATE_CMD'], self.env['ESVN_OPTIONS'], self.env['ESVN_STORE_DIR'])
		if self.env['ESVN_USER']:
			cmd += ' --user "%s" --password "%s"' % (self.env['ESVN_USER'], self.env['ESVN_PASSWORD'])
		return cmd

vcsl = [GitSupport, HgSupport, SvnSupport]

def main(argv):
	vcsnames = [x.inherit for x in vcsl]
	opt = OptionParser(
			usage='%prog [options] -- [emerge options]',
			version='%%prog %s' % PV,
			description='Enumerate all live packages in system, check their repositories for updates and remerge the updated ones. Supported VCS-es: %s.' % ', '.join(vcsnames)
	)
	opt.add_option('-c', '--config-file', action='store', dest='config_file',
		help='Configuration file (default: /etc/portage/smart-live-rebuild.conf)')
	opt.add_option('-C', '--no-color', action='store_false', dest='color',
		help='Disable colorful output.')
	opt.add_option('-E', '--no-erraneous-merge', action='store_false', dest='erraneous_merge',
		help='Disable emerging packages for which the update has failed.')
	opt.add_option('-j', '--jobs', action='store', type='int', dest='jobs',
		help='Spawn JOBS parallel processes to perform repository updates.')
	opt.add_option('-l', '--local-rev', action='store_true', dest='local_rev',
		help='Force determining the current package revision from the repository instead of using the one saved by portage.')
	opt.add_option('-N', '--no-network', action='store_false', dest='network',
		help='Disable network interaction and just aggregate already updated repositories (requires --local-rev not set).')
	opt.add_option('-O', '--no-offline', action='store_false', dest='offline',
		help='Disable setting ESCM_OFFLINE for emerge.')
	opt.add_option('-p', '--pretend', action='store_true', dest='pretend',
		help='Only print a list of the packages which were updated; do not call emerge to rebuild them.')
	opt.add_option('-P', '--profile', action='store', dest='profile',
		help='Configuration profile (config file section) to use (default: smart-live-rebuild)')
	opt.add_option('-Q', '--quickpkg', action='store_true', dest='quickpkg',
		help='Call quickpkg to create binary backups of packages which are going to be updated.')
	opt.add_option('-S', '--no-setuid', action='store_false', dest='setuid',
		help='Do not switch UID to portage when FEATURES=userpriv is set.')
	opt.add_option('-t', '--type', action='append', type='choice', choices=vcsnames, dest='type',
		help='Limit rebuild to packages using specific VCS. If used multiple times, all specified VCS-es will be used.')
	opt.add_option('-U', '--unprivileged-user', action='store_true', dest='unprivileged_user',
		help='Allow running as an unprivileged user.')

	# Config&option parsing algo:
	# 1) set default configfile,
	# 2) parse opts to get configfile,
	# 3) set configfile defaults if applicable,
	# 4) reparse opts.
	defs = {
		'color': 'True',
		'erraneous_merge': 'True',
		'jobs': '1',
		'local_rev': 'False',
		'network': 'True',
		'offline': 'True',
		'pretend': 'False',
		'quickpkg': 'False',
		'setuid': str('userpriv' in portage.settings.features),
		'type': '',
		'unprivileged_user': 'False'
	}

	opt.set_defaults(
			config_file = '/etc/portage/smart-live-rebuild.conf',
			profile = 'smart-live-rebuild'
	)
	c = ConfigParser(defs)
	(opts, args) = opt.parse_args(argv[1:])

	# now look for the config file(s)
	cfl = [opts.config_file]
	sect = opts.profile
	try:
		while cfl[-1] != '' and c.read(os.path.expanduser(cfl[-1])):
			# config file chaining support
			try:
				cf = c.get(sect, 'config_file')
			except NoOptionError:
				break
			else:
				if cf not in cfl:
					cfl.append(cfl)
				else:
					break
	except Exception as e:
		out.err('Error while parsing configuration file:')
		out.err('%s: %s' % (e.__class__.__name__, e))

	# we need to have it to get the defaults
	if not c.has_section(sect):
		c.add_section(sect)

	# set defaults from the configfile and parse
	newdefs = {}
	deftypes = None
	for k, defv in defs.items():
		if defs[k] in ('True', 'False'): # bool
			try:
				newdefs[k] = c.getboolean(sect, k)
			except ValueError:
				out.err('Incorrect boolean value: %s=%s' % (k, c.get(sect, k)))
				newdefs[k] = (defv == 'True')
		elif k == 'jobs': # int
			try:
				newdefs[k] = c.getint(sect, k)
			except ValueError:
				out.err('Incorrect int value: %s=%s' % (k, c.get(sect, k)))
				newdefs[k] = int(defv)
		elif k == 'type':
			# this one needs special handling due to the append action
			t = c.get(sect, k)
			if t != '':
				deftypes = t.split(',')
				for t in list(deftypes):
					if t not in vcsnames:
						out.err('Incorrect value within %s: %s' % (k, t))
						deftypes.remove(t)
		else:
			newdefs[k] = c.get(sect, k)
		
	opt.set_defaults(**newdefs)
	(opts, args) = opt.parse_args(argv[1:])
	if not opts.type:
		opts.type = deftypes
	Shared.opts = opts

	if not opts.color:
		out.monochromize()

	if not opts.pretend:
		try:
			import psutil

			def getproc(pid):
				for ps in psutil.get_process_list():
					if pid == ps.pid:
						return ps
				raise Exception()

			def getscriptname(ps):
				if os.path.basename(ps.cmdline[0]) != ps.name:
					return ps.cmdline[0]
				cmdline = ps.cmdline[1:]
				while cmdline[0].startswith('-'): # omit options
					cmdline.pop(0)
				return os.path.basename(cmdline[0])

			ps = getproc(os.getppid())
			# traverse upstream to find the emerge process
			while ps.pid > 1:
				if getscriptname(ps) == 'emerge':
					out.s1('Running under the emerge process, assuming --pretend.')
					opts.pretend = True
					break
				ps = ps.parent
		except Exception:
			pass

	if opts.setuid and 'userpriv' not in portage.settings.features:
		out.err('setuid requested but FEATURES=userpriv not set, assuming --no-setuid.')
		opts.setuid = False
	if opts.local_rev and not opts.network:
		out.err('The --local-rev and --no-network options can not be specified together.')
		return 1
	if opts.jobs <= 0:
		out.err('The argument to --jobs option must be a positive integer.')
		return 1
	elif opts.jobs > 1 and not opts.network:
		out.s1('Using parallel jobs with --no-network is inefficient, assuming no --jobs.')
		opts.jobs = 1

	childpid = None
	commpipe = None
	userok = (os.geteuid() == 0)
	if opts.setuid:
		puid = portage.data.portage_uid
		pgid = portage.data.portage_gid
		if puid and pgid:
			if not userok:
				if os.getuid() == puid:
					if not opts.pretend:
						out.s1('Running as the portage user, assuming --pretend.')
						opts.pretend = True
					if opts.quickpkg:
						out.err("Running as the portage user, --quickpkg probably won't work")
					userok = True
			elif opts.pretend and not opts.quickpkg:
				out.s1('Dropping superuser privileges ...')
				os.setuid(puid)
			else:
				out.s1('Forking to drop superuser privileges ...')
				commpipe = os.pipe()
				childpid = os.fork()
		else:
			out.err("'userpriv' is set but there's no 'portage' user in the system")

	if not opts.unprivileged_user and not userok:
		out.err('Either superuser or portage privileges are required!')
		out.out('''
This tool requires either superuser or portage (if FEATURES=userpriv is set)
privileges. If you would like to force running the update using your current
user account, please pass the --unprivileged-user option.
''')
		return 1

	try:
		if not childpid:
			if childpid == 0:
				os.close(commpipe[0])
				os.setuid(puid)
			if opts.type:
				vcslf = [x for x in vcsl if x.inherit in opts.type]
			else:
				vcslf = vcsl

			out.s1('Enumerating the packages ...')

			erraneous = []
			rebuilds = {}

			Shared.opentmp()
			try:
				db = portage.db[portage.settings['ROOT']]['vartree'].dbapi
				for cpv in db.cpv_all():
					try:
						inherits = db.aux_get(cpv, ['INHERITED'])[0].split()

						for vcs in vcslf:
							if vcs.match(inherits):
								env = bz2.BZ2File('%s/environment.bz2' % db.getpath(cpv), 'r')
								vcs = vcs(cpv, env)
								env.close()
								if opts.network or vcs.getsavedrev():
									dir = vcs.getpath()
									if dir not in rebuilds:
										rebuilds[dir] = vcs
									else:
										rebuilds[dir].append(vcs)
					except KeyboardInterrupt:
						raise
					except NonLiveEbuild as e:
						out.err('%s: %s' % (cpv, e))
					except Exception as e:
						out.err('Error enumerating %s: [%s] %s' % (cpv, e.__class__.__name__, e))
						erraneous.append(cpv)
			finally:
				Shared.closetmp()

			if opts.jobs == 1:
				out.s1('Updating the repositories...')
			else:
				out.s1('Updating the repositories using %s%d%s parallel jobs...' % (out.white, opts.jobs, out.s1reset))
			packages = []

			processes = []
			items = list(rebuilds.items())
			while True:
				try:
					if len(processes) < opts.jobs and len(items) > 0:
						(dir, vcs) = items.pop(0)
						try:
							vcs.startupdate()
							if opts.jobs == 1:
								ret = vcs.endupdate(True)
							else:
								processes.append(vcs)
						except KeyboardInterrupt:
							vcs.abortupdate()
							raise
					elif len(processes) == 0: # which is true if jobs == 1 too
						break
					else:
						time.sleep(0.3)

					for vcs in processes:
						ret = vcs.endupdate()
						if ret is not None:
							processes.remove(vcs)
							break

					if ret:
						packages.extend(vcs.cpv)
				except KeyboardInterrupt:
					out.err('Updates interrupted, proceeding with already updated repos.')
					for vcs in processes:
						vcs.abortupdate()
					break
				except Exception as e:
					out.err('Error updating %s: [%s] %s' % (vcs.cpv, e.__class__.__name__, e))
					if opts.jobs != 1 and vcs in processes:
						processes.remove(vcs)
					erraneous.extend(vcs.cpv)

			if childpid == 0:
				pdata = {'packages': packages, 'erraneous': erraneous}
				pipe = os.fdopen(commpipe[1], 'wb')
				pickle.dump(pdata, pipe, pickle.HIGHEST_PROTOCOL)
				return 0
		else:
			os.close(commpipe[1])
			pipe = os.fdopen(commpipe[0], 'rb')
			sigint = signal.getsignal(signal.SIGINT)
			signal.signal(signal.SIGINT, signal.SIG_IGN)
			try:
				pdata = pickle.load(pipe)
			except EOFError: # child terminated early
				return 1
			signal.signal(signal.SIGINT, sigint)
			packages = pdata['packages']
			erraneous = pdata['erraneous']

		if opts.erraneous_merge and len(erraneous) > 0:
			packages.extend(erraneous)

		if opts.quickpkg and len(packages) >= 1:
			out.s1('Calling quickpkg to create %s%d%s binary packages ...' % (out.white, len(packages), out.s1reset))
			cmd = ['/usr/sbin/quickpkg', '--include-config=y']
			cmd.extend(['=%s' % x for x in packages])
			out.s2(' '.join(cmd))
			subprocess.Popen(cmd, stdout=sys.stderr).wait()

		if len(packages) < 1:
			out.s1('No updates found')
		elif opts.pretend:
			out.s1('Printing a list of updated packages ...')
			if opts.erraneous_merge and len(erraneous) > 0:
				out.s2('(please notice that it contains the update-failed ones as well)')
			for p in packages:
				print('>=%s' % p)
		else:
			if opts.erraneous_merge and len(erraneous) > 0:
				if opts.offline:
					out.s1('Merging update-failed packages, assuming --no-offline.')
					opts.offline = False

			out.s1('Calling emerge to rebuild %s%d%s packages ...' % (out.white, len(packages), out.s1reset))
			if opts.offline:
				os.putenv('ESCM_OFFLINE', 'true')
			cmd = ['emerge', '--oneshot']
			cmd.extend(args)
			cmd.extend(['>=%s' % x for x in packages])
			out.s2(' '.join(cmd))
			os.execv('/usr/bin/emerge', cmd)
	finally:
		if childpid: # make sure that we leave no orphans
			os.kill(childpid, signal.SIGTERM)

	return 0

if __name__ == '__main__':
	sys.exit(main(sys.argv))
