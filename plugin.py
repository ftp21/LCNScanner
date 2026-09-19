# Based on a plugin from Sif Team.
# This version created by IanSav and the OpenATV team.
# Modified to allow interactive resolution of duplicate LCNs before the
# bouquets are written, instead of always picking the strongest signal.

import gettext
import json
from os import environ
from os.path import join
from sys import maxsize

from enigma import eDVBDB, eServiceCenter, eServiceReference, eTimer

from Components.ActionMap import HelpableActionMap
from Components.config import ConfigSelection, ConfigSubsection, ConfigText, ConfigYesNo, config, getConfigListEntry
from Components.ConfigList import ConfigListScreen
from Components.Language import language
from Components.PluginComponent import plugins
from Components.Sources.StaticText import StaticText
from Plugins.Plugin import PluginDescriptor
from Screens.MessageBox import MessageBox
from Screens.Screen import Screen
from Screens.Setup import Setup
from Tools.Directories import SCOPE_CONFIG, SCOPE_PLUGIN_ABSOLUTE, SCOPE_PLUGINS, fileReadLines, fileReadXML, fileWriteLines, resolveFilename

MODULE_NAME = __name__.split(".")[-1]

MAX_BOUQUET_SERVICES = 1099  # Practical limit on the number of services in a single bouquet.

# Filename of the small JSON manifest listing every bouquet this plugin has
# ever created or overwritten (see LCNScanner._rememberManagedBouquet() and
# writeBouquet()). Kept as a free function (hasManagedBouquetInstalled(),
# further down) rather than only a method, so another plugin (e.g.
# SettingsHub, to decide whether a channel-list replacement should be
# followed by a DVB-T rescan + LCN rebuild) can check it without needing to
# know this plugin's bouquet-naming conventions or instantiate LCNScanner.
MANAGED_BOUQUETS_FILENAME = "lcnscanner_managed_bouquets.json"

# Per-plugin translation domain. Screens.Setup reads this same name to translate
# this plugin's own setup.xml (item text/description), while our own _() below
# covers the strings used directly in this file (ConfigSelection choices, screen
# titles, button labels, etc). Without this, only the strings that happen to
# already exist in OpenATV's own translation catalogue show up in Italian, and
# anything added here shows up in English regardless of the active language.
PluginLanguageDomain = "LCNScanner"
PluginLanguagePath = "SystemPlugins/LCNScanner/locale"


def localeInit():
	environ["LANGUAGE"] = language.getLanguage()[:2]
	gettext.bindtextdomain(PluginLanguageDomain, resolveFilename(SCOPE_PLUGINS, PluginLanguagePath))


def _(txt):
	translated = gettext.dgettext(PluginLanguageDomain, txt)
	return translated if translated != txt else gettext.gettext(txt)


localeInit()
language.addCallback(localeInit)

config.plugins.LCNScanner = ConfigSubsection()
config.plugins.LCNScanner.showInPluginsList = ConfigYesNo(default=False)
config.plugins.LCNScanner.showInPluginsList.addNotifier(plugins.reloadPlugins, initial_call=False, immediate_feedback=False)


class LCNScanner:
	MODE_TV = "1:7:1:0:0:0:0:0:0:0:(type == 1) || (type == 17) || (type == 22) || (type == 25) || (type == 134) || (type == 195)"
	MODE_RADIO = "1:7:2:0:0:0:0:0:0:0:(type == 2) || (type == 10)"
	MODES = {
		"TV": (1, 17, 22, 25, 134, 195),
		"Radio": (2, 10)
	}

	OLDDB_NAMESPACE = 0
	OLDDB_ONID = 1
	OLDDB_TSID = 2
	OLDDB_SID = 3
	OLDDB_LCN = 4
	OLDDB_SIGNAL = 5

	DB_SID = 0
	DB_TSID = 1
	DB_ONID = 2
	DB_NAMESPACE = 3
	DB_SIGNAL = 4
	DB_LCN_BROADCAST = 5
	DB_LCN_SCANNED = 6
	DB_LCN_GUI = 7
	DB_PROVIDER = 8  # Max 255 characters.
	DB_PROVIDER_GUI = 9
	DB_SERVICENAME = 10  # Max 255 characters.
	DB_SERVICENAME_GUI = 11

	LCNS_MEDIUM = 0
	LCNS_TRIPLET = 1
	LCNS_SERVICEREFERENCE = 2
	LCNS_SIGNAL = 3
	LCNS_LCN_BROADCAST = 4
	LCNS_LCN_SCANNED = 5
	LCNS_LCN_GUI = 6
	LCNS_PROVIDER = 7
	LCNS_PROVIDER_GUI = 8
	LCNS_SERVICENAME = 9
	LCNS_SERVICENAME_GUI = 10

	SERVICE_PROVIDER = 0
	SERVICE_SERVICEREFERENCE = 1
	SERVICE_NAME = 2

	def __init__(self):
		self.configPath = resolveFilename(SCOPE_CONFIG)
		self.ruleList = {}
		self.rulesDom = fileReadXML(resolveFilename(SCOPE_PLUGIN_ABSOLUTE, "rules.xml"), default="<rulesxml />", source=MODULE_NAME)
		if self.rulesDom is not None:
			rulesIndex = 1
			for rules in self.rulesDom.findall("rules"):
				name = rules.get("name")
				if name:
					self.ruleList[name] = name
				else:
					name = f"Rules{rulesIndex}"
					rules.set("name", name)
					self.ruleList[name] = name
					rulesIndex += 1
		config.plugins.LCNScanner.rules = ConfigSelection(default="Default", choices=self.ruleList)
		config.plugins.LCNScanner.useSpacerLines = ConfigYesNo(default=False)
		config.plugins.LCNScanner.addServiceNames = ConfigYesNo(default=False)
		config.plugins.LCNScanner.useDescriptionLines = ConfigYesNo(default=False)
		# Bouquet destination/position/name are persistent settings shown on the main
		# setup screen, so the user configures them once instead of being asked in a
		# popup after every scan.
		config.plugins.LCNScanner.bouquetName = ConfigText(default="", fixed_size=False)
		config.plugins.LCNScanner.bouquetDestination = ConfigSelection(default="new", choices=[("new", _("Create a new bouquet")), ("existing", _("Overwrite an existing bouquet"))])
		config.plugins.LCNScanner.bouquetPosition = ConfigSelection(default="tail", choices=[("head", _("At the start of the bouquet list")), ("tail", _("At the end of the bouquet list"))])
		existingBouquets = self.listExistingBouquets("TV") + self.listExistingBouquets("Radio")
		targetChoices = existingBouquets if existingBouquets else [("", _("No existing bouquets found"))]
		config.plugins.LCNScanner.bouquetTarget = ConfigSelection(default=targetChoices[0][0], choices=targetChoices)
		self.scanState = None  # Populated by prepareScan(), consumed by finishScan().
		self.scanTimers = []  # Keeps loadServicesAsync()'s timers alive while a scan is in progress.

	@staticmethod
	def getModes(element):
		mode = element.get("mode", "All")
		match mode:
			case "All" | "Both":
				modes = ("TV", "Radio")
			case "TV":
				modes = ("TV",)
			case "Radio":
				modes = ("Radio",)
			case _:
				print(f"[LCNScanner] Error: Invalid mode '{mode}' specified, 'All' assumed!  (Only 'All', 'Both', 'Radio' or 'TV' permitted.)")
				modes = ("TV", "Radio")
		return modes

	def loadLCNs(self):
		print("[LCNScanner] Loading 'lcndb' file.")
		lcndb = []
		for lcn in fileReadLines(join(self.configPath, "lcndb"), default=[], source=MODULE_NAME):
			if lcn not in lcndb:
				lcndb.append(lcn)
			else:
				print(f"[LCNScanner] Error: Duplicated line detected in lcndb!  ({lcn}).")
		return lcndb

	CHOICES_FILENAME = "lcnscanner_choices.json"

	def _choiceMapPath(self):
		return join(self.configPath, self.CHOICES_FILENAME)

	def _loadChoiceMap(self):
		# {"mode|medium|lcn": "sid:tsid:onid:namespace", ...}. Keyed by triplet
		# (not by candidate index) so the memory survives a rescan even if the
		# same duplicate's candidates come back in a different order.
		try:
			with open(self._choiceMapPath(), "r", encoding="utf-8") as f:
				data = json.load(f)
			return data if isinstance(data, dict) else {}
		except (OSError, ValueError):
			return {}

	def _saveChoiceMap(self, mapping):
		try:
			with open(self._choiceMapPath(), "w", encoding="utf-8") as f:
				json.dump(mapping, f)
		except OSError as err:
			print(f"[LCNScanner] Error: Could not save duplicate-resolution memory!  ({err})")

	@staticmethod
	def _choiceKeyString(key):
		mode, medium, lcn = key
		return f"{mode}|{medium}|{lcn}"

	def _savedChoiceIndex(self, key, candidates):
		triplet = self._loadChoiceMap().get(self._choiceKeyString(key))
		if triplet is None:
			return None
		for index, candidate in enumerate(candidates):
			if candidate[self.LCNS_TRIPLET] == triplet:
				return index
		return None  # The previously chosen service is no longer one of the candidates.

	def _rememberChoice(self, key, triplet):
		mapping = self._loadChoiceMap()
		stringKey = self._choiceKeyString(key)
		if mapping.get(stringKey) != triplet:
			mapping[stringKey] = triplet
			self._saveChoiceMap(mapping)

	def _rememberManagedBouquet(self, bouquetName):
		# See MANAGED_BOUQUETS_FILENAME and hasManagedBouquetInstalled() above.
		path = join(self.configPath, MANAGED_BOUQUETS_FILENAME)
		try:
			with open(path, "r", encoding="utf-8") as f:
				names = json.load(f)
			if not isinstance(names, list):
				names = []
		except (OSError, ValueError):
			names = []
		if bouquetName not in names:
			names.append(bouquetName)
			try:
				with open(path, "w", encoding="utf-8") as f:
					json.dump(names, f)
			except OSError as err:
				print(f"[LCNScanner] Error: Could not update the managed-bouquets list!  ({err})")

	def loadServices(self, mode):
		print(f"[LCNScanner] Loading {mode} services.")
		services = {}
		serviceHandler = eServiceCenter.getInstance()
		match mode:
			case "TV":
				providerQuery = f"{self.MODE_TV} FROM PROVIDERS ORDER BY name"
			case "Radio":
				providerQuery = f"{self.MODE_RADIO} FROM PROVIDERS ORDER BY name"
		providers = serviceHandler.list(eServiceReference(providerQuery))
		if providers:
			for serviceQuery, providerName in providers.getContent("SN", True):
				serviceList = serviceHandler.list(eServiceReference(serviceQuery))
				for serviceReference, serviceName in serviceList.getContent("SN", True):
					services[":".join(serviceReference.split(":")[3:7])] = (providerName, serviceReference, serviceName)
		return services

	def loadServicesAsync(self, mode, callback, batchSize=5):
		# Same result as loadServices(), but walks the providers in small batches via
		# a repeating zero-delay timer instead of one long synchronous loop. On a
		# large lineup (many satellite/cable providers), loadServices() can block the
		# main loop for long enough to visibly disrupt whatever is currently playing
		# (frozen video, a dropped CI/CAM channel); yielding between batches avoids
		# that while still finishing in roughly the same total time.
		print(f"[LCNScanner] Loading {mode} services.")
		services = {}
		serviceHandler = eServiceCenter.getInstance()
		match mode:
			case "TV":
				providerQuery = f"{self.MODE_TV} FROM PROVIDERS ORDER BY name"
			case "Radio":
				providerQuery = f"{self.MODE_RADIO} FROM PROVIDERS ORDER BY name"
		providers = serviceHandler.list(eServiceReference(providerQuery))
		iterator = iter(providers.getContent("SN", True) if providers else [])
		timer = eTimer()
		self.scanTimers.append(timer)  # Keep a reference so the timer isn't garbage collected mid-scan.

		def processBatch():
			for _ in range(batchSize):
				try:
					serviceQuery, providerName = next(iterator)
				except StopIteration:
					self.scanTimers.remove(timer)
					callback(services)
					return
				serviceList = serviceHandler.list(eServiceReference(serviceQuery))
				for serviceReference, serviceName in serviceList.getContent("SN", True):
					services[":".join(serviceReference.split(":")[3:7])] = (providerName, serviceReference, serviceName)
			timer.start(0, True)

		timer.callback.append(processBatch)
		timer.start(0, True)

	def parseLCNEntries(self, mode, lcndb, services):
		print(f"[LCNScanner] Matching LCN entries with {mode} services.")
		lcns = []
		try:
			version = int(lcndb[0][9:]) if lcndb[0].startswith("#VERSION ") else 1
		except Exception:
			version = 1
		match version:
			case 1:
				for line in lcndb:
					line = line.strip()
					if len(line) != 38:
						continue
					item = line.split(":")
					if len(item) != 6:
						continue
					match item[self.OLDDB_NAMESPACE][:4].upper():
						case "DDDD":
							medium = "A"
						case "EEEE":
							medium = "T"
						case "FFFF":
							medium = "C"
						case _:
							medium = "S"
					service = f"{item[self.OLDDB_SID].lstrip("0")}:{item[self.OLDDB_TSID].lstrip("0")}:{item[self.OLDDB_ONID].lstrip("0")}:{item[self.OLDDB_NAMESPACE].lstrip("0")}".upper()
					lcns.append([
						medium,
						service,
						services[service][self.SERVICE_SERVICEREFERENCE] if service in services else "",
						int(item[self.OLDDB_SIGNAL]),
						int(item[self.OLDDB_LCN]),
						0,
						0,
						services[service][self.SERVICE_PROVIDER] if service in services else "",
						"",
						services[service][self.SERVICE_NAME] if service in services else "",
						""
					])
			case 2:
				for line in lcndb:
					if line.startswith("#"):
						continue
					item = line.split(":")
					match item[self.DB_NAMESPACE][:4]:
						case "DDDD":
							medium = "A"
						case "EEEE":
							medium = "T"
						case "FFFF":
							medium = "C"
						case _:
							medium = "S"
					service = f"{item[self.DB_SID]}:{item[self.DB_TSID]}:{item[self.DB_ONID]}:{item[self.DB_NAMESPACE]}"
					lcns.append([
						medium,
						service,
						services[service][self.SERVICE_SERVICEREFERENCE] if service in services else "",
						int(item[self.DB_SIGNAL]),
						int(item[self.DB_LCN_BROADCAST]),
						int(item[self.DB_LCN_SCANNED]),
						int(item[self.DB_LCN_GUI]),
						services[service][self.SERVICE_PROVIDER] if service in services else "",
						item[self.DB_PROVIDER_GUI],
						services[service][self.SERVICE_NAME] if service in services else "",
						item[self.DB_SERVICENAME_GUI]
					])
			case _:
				print("[LCNScanner] Error: LCN db file format unrecognized!")
		return lcns

	def buildConflictGroups(self, mode, lcns, services):
		# Group all entries sharing the same (medium, broadcast LCN) so a decision
		# can be made once per group, instead of the previous streaming approach
		# where the outcome depended on processing order.
		groups = {}
		for data in lcns:
			service = data[self.LCNS_TRIPLET]
			serviceReference = data[self.LCNS_SERVICEREFERENCE].split(":")
			lcn = data[self.LCNS_LCN_BROADCAST]
			medium = data[self.LCNS_MEDIUM]
			if service in services:  # The service represented by this LCN entry is still a valid service.
				groups.setdefault((medium, lcn), []).append(data)
			elif len(serviceReference) > 2 and serviceReference[2] in self.MODES[mode]:  # Skip all LCN entries of the same type that are not a valid service.
				print(f"[LCNScanner] Service '{service}' with LCN {lcn} not a valid {mode} service!")
		return groups

	def resolveConflicts(self, mode, groups, duplicate, renumbers, choices, remember=False):
		# choices: {(mode, medium, lcn): candidateIndex, ...} coming from the user's
		# selection in LCNScannerDuplicates. A group not present in choices falls
		# back to whatever was remembered from a previous run for that same slot
		# (see _savedChoiceIndex()), and only then to "strongest signal wins" if
		# nothing was ever remembered either. remember=True (only passed from
		# finishScan(), never from the prepareScan() preview) persists the
		# winning candidate's identity so an unattended rerun - e.g. the one the
		# stock ServiceScan screen triggers automatically after any channel scan,
		# see lcnScan() - resolves the same way a duplicate was resolved last time
		# instead of silently reverting to strongest-signal.
		scannerLast = duplicate[mode][1]
		cableLCNs = {}
		satelliteLCNs = {}
		terrestrialLCNs = {}
		mediumTargets = {"C": cableLCNs, "S": satelliteLCNs, "A": terrestrialLCNs, "T": terrestrialLCNs}
		# Duplicates are renumbered starting from the same configured range, but
		# each OUTPUT list (cable/satellite/terrestrial - "A" and "T" share the
		# same terrestrialLCNs dict and so the same counter) gets its own counter,
		# keyed by that dict's identity. Each is written as its own separate
		# bouquet, so a slot only needs to be unique within its own list, not
		# across all of them. A single counter shared across every medium (sorted
		# "C" < "S" < "T", so satellite is always processed before terrestrial)
		# meant a lineup with many satellite duplicates could exhaust a range like
		# 600-699 - and run into the thousands - before terrestrial's own (much
		# rarer) duplicates were ever reached, e.g. terrestrial LCN 3 ending up
		# renumbered to 16635 instead of 600.
		scannerLCNByTarget = {id(cableLCNs): duplicate[mode][0], id(satelliteLCNs): duplicate[mode][0], id(terrestrialLCNs): duplicate[mode][0]}
		conflicts = []

		def applyRenumber(lcn, data, serviceLCNs):
			for renumber in renumbers[mode]:
				if renumber[0][0] <= lcn <= renumber[0][1]:
					try:
						startingLCN = lcn
						lcn = int(eval(renumber[1].replace("LCN", str(lcn))))
						print(f"[LCNScanner] LCN {startingLCN} is renumbered to {lcn} via rule range {renumber[0][0]}-{renumber[0][1]} and formula='{renumber[1]}'.")
						if lcn in serviceLCNs:
							scannerLCN = scannerLCNByTarget[id(serviceLCNs)]
							print(f"[LCNScanner] Renumbered LCN {startingLCN} is now a duplicated LCN {lcn}, renumbering {startingLCN} to {scannerLCN}.")
							data[self.LCNS_LCN_SCANNED] = scannerLCN
							lcn = scannerLCN
							scannerLCNByTarget[id(serviceLCNs)] += 1
						else:
							data[self.LCNS_LCN_SCANNED] = lcn
					except ValueError as err:
						print(f"[LCNScanner] Error: LCN renumber formula '{renumber[1]}' is invalid!  ({err})")
			return lcn

		for (medium, lcn), candidates in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
			serviceLCNs = mediumTargets[medium]
			if len(candidates) == 1:
				data = list(candidates[0])
				data[self.LCNS_LCN_SCANNED] = lcn
				finalLcn = applyRenumber(lcn, data, serviceLCNs)
				serviceLCNs[finalLcn] = tuple(data)
				continue
			# Duplicate LCN: reserve the slots for the "losers" up front so that
			# changing the winner later (from the UI) never changes anyone else's slot.
			key = (mode, medium, lcn)
			winnerIndex = choices.get(key)
			if winnerIndex is None:
				winnerIndex = self._savedChoiceIndex(key, candidates)
			if winnerIndex is None or winnerIndex >= len(candidates):
				winnerIndex = max(range(len(candidates)), key=lambda i: candidates[i][self.LCNS_SIGNAL])
			if remember:
				self._rememberChoice(key, candidates[winnerIndex][self.LCNS_TRIPLET])
			reserved = []
			scannerLCN = scannerLCNByTarget[id(serviceLCNs)]
			if scannerLCN > scannerLast:
				print(f"[LCNScanner] Warning: Duplicate LCN {lcn} found but duplicate LCN range exhausted!")
			else:
				for _ in range(len(candidates) - 1):
					reserved.append(scannerLCN)
					scannerLCN += 1
				scannerLCNByTarget[id(serviceLCNs)] = scannerLCN
			reservedIter = iter(reserved)
			for i, candidate in enumerate(candidates):
				data = list(candidate)
				if i == winnerIndex:
					data[self.LCNS_LCN_SCANNED] = lcn
					finalLcn = applyRenumber(lcn, data, serviceLCNs)
				else:
					slot = next(reservedIter, lcn)  # Falls back to the contested LCN only if the range was exhausted.
					if slot != lcn:
						print(f"[LCNScanner] Duplicate LCN found, renumbering {lcn} to {slot}.")
					data[self.LCNS_LCN_SCANNED] = slot
					finalLcn = applyRenumber(slot, data, serviceLCNs)
				serviceLCNs[finalLcn] = tuple(data)
			conflicts.append({
				"mode": mode,
				"medium": medium,
				"lcn": lcn,
				"candidates": [(f"{c[self.LCNS_SERVICENAME] or c[self.LCNS_TRIPLET]} ({c[self.LCNS_PROVIDER]})" if c[self.LCNS_PROVIDER] else (c[self.LCNS_SERVICENAME] or c[self.LCNS_TRIPLET]), c) for c in candidates],
				"default": winnerIndex,
			})
		return cableLCNs, satelliteLCNs, terrestrialLCNs, conflicts

	def listExistingBouquets(self, mode):
		# Returns [(bouquetFileName, displayName), ...] for every user bouquet
		# currently listed in bouquets.tv / bouquets.radio, so the user can pick
		# one to overwrite instead of creating a new one.
		extension = mode.lower()
		bouquets = []
		bouquetsPath = join(self.configPath, f"bouquets.{extension}")
		for line in fileReadLines(bouquetsPath, default=[], source=MODULE_NAME):
			line = line.strip()
			if not line.startswith("#SERVICE"):
				continue
			data = line.split(":")[-1]
			if not data.startswith("FROM BOUQUET "):
				continue
			data = data[13:].strip()
			endPos = data.find("\"", 1) if data.startswith("\"") else data.find(" ")
			bouquetFile = data[1:endPos] if data.startswith("\"") else data
			if not bouquetFile:
				continue
			for bouquetLine in fileReadLines(join(self.configPath, bouquetFile), default=[], source=MODULE_NAME):
				if bouquetLine.startswith("#NAME "):
					bouquets.append((bouquetFile, bouquetLine[6:].strip()))
					break
		return bouquets

	def writeBouquet(self, mode, medium, serviceLCNs, markers, bouquetTarget=None):
		def insertMarker(mode, lcn):
			if lcn in markers[mode]:
				bouquet.append(f"#SERVICE 1:64:0:0:0:0:0:0:0:0::{markers[mode][lcn]}")
				if useDescriptionLines:
					bouquet.append(f"#DESCRIPTION {markers[mode][lcn]}")
			return bouquet

		bouquetTarget = bouquetTarget or {"destination": "new", "position": "tail"}
		useDescriptionLines = config.plugins.LCNScanner.useDescriptionLines.value if config.plugins.LCNScanner.addServiceNames.value else False
		bouquet = []
		overwriteExisting = bouquetTarget.get("destination") == "existing" and bouquetTarget.get("target")
		existingName = None
		if overwriteExisting:
			bouquetName = bouquetTarget["target"]
			for line in fileReadLines(join(self.configPath, bouquetName), default=[], source=MODULE_NAME):
				if line.startswith("#NAME "):
					existingName = line[6:].strip()
					break
		else:
			bouquetName = f"userbouquet.{medium.lower()}_lcn.{mode.lower()}"
		self._rememberManagedBouquet(bouquetName)
		displayName = existingName if existingName else (bouquetTarget.get("name") or f"{medium} {mode} LCN")
		bouquet.append(f"#NAME {displayName}")
		bouquet.append(f"#SERVICE 1:64:0:0:0:0:0:0:0:0::{displayName}")
		if useDescriptionLines:
			bouquet.append(f"#DESCRIPTION {displayName}")
		index = 0
		useSpacerLines = config.plugins.LCNScanner.useSpacerLines.value
		serviceCount = 0
		for lcn in sorted(serviceLCNs.keys()):
			index += 1
			while lcn > index:
				bouquet = insertMarker(mode, index)
				if useSpacerLines:
					bouquet.append("#SERVICE 1:832:D:0:0:0:0:0:0:0:")
				index += 1
			bouquet = insertMarker(mode, index)
			name = serviceLCNs[lcn][self.LCNS_SERVICENAME]
			serviceName = f":{name}" if config.plugins.LCNScanner.addServiceNames.value else ""
			bouquet.append(f"#SERVICE {serviceLCNs[lcn][self.LCNS_SERVICEREFERENCE]}{serviceName}")
			serviceCount += 1
			if useDescriptionLines:
				bouquet.append(f"#DESCRIPTION {name}")
		if serviceCount > MAX_BOUQUET_SERVICES:
			print(f"[LCNScanner] Warning: Bouquet '{displayName}' contains {serviceCount} services, above the recommended limit of {MAX_BOUQUET_SERVICES}.")
		# Save bouquet content. When overwriting an existing bouquet its entry in
		# bouquets.{extension} is already correct and is left untouched; a newly
		# created bouquet is registered at the head or tail of that list.
		extension = mode.lower()
		bouquetsPath = join(self.configPath, bouquetName)
		if fileWriteLines(bouquetsPath, bouquet, source=MODULE_NAME):
			print(f"[LCNScanner] Bouquet '{bouquetsPath}' saved.")
		else:
			print(f"[LCNScanner] Error: Bouquet '{bouquetsPath}' could not be saved!")
		if overwriteExisting:
			return
		bouquetsPath = join(self.configPath, f"bouquets.{extension}")
		bouquets = fileReadLines(bouquetsPath, default=[], source=MODULE_NAME)
		# Always drop any pre-existing reference to this bouquet before re-adding it,
		# so the requested head/tail position is honoured even on a rescan, instead of
		# leaving a previously created entry stuck wherever it was first inserted.
		alreadyListed = any(f"\"{bouquetName}\"" in line for line in bouquets)
		bouquets = [line for line in bouquets if f"\"{bouquetName}\"" not in line]
		reference = f"#SERVICE 1:7:2:0:0:0:0:0:0:0:FROM BOUQUET \"{bouquetName}\" ORDER BY bouquet"
		if bouquetTarget.get("position") == "head":
			# Insert after the list's own #NAME header line (if present) so that line
			# stays first in the file, as it should be, rather than being pushed down.
			insertAt = 1 if bouquets and bouquets[0].startswith("#NAME ") else 0
			bouquets.insert(insertAt, reference)
		else:
			bouquets.append(reference)
		if fileWriteLines(bouquetsPath, bouquets, source=MODULE_NAME):
			action = "repositioned in" if alreadyListed else "added to"
			print(f"[LCNScanner] Bouquet '{bouquetName}' {action} '{bouquetsPath}' ({bouquetTarget.get('position', 'tail')}).")
		else:
			print(f"[LCNScanner] Error: Bouquet '{bouquetName}' could not be added to '{bouquetsPath}'!")

	def buildLCNs(self, serviceLCNs):
		lcndb = []
		for lcn in sorted(serviceLCNs.keys()):
			data = []
			for field in (self.LCNS_TRIPLET, self.LCNS_SIGNAL, self.LCNS_LCN_BROADCAST, self.LCNS_LCN_SCANNED, self.LCNS_LCN_GUI):
				data.append(str(serviceLCNs[lcn][field]))
			data.extend(["", "", "", ""])  # This keeps the record length as defined while all the fields are not available.
			lcndb.append(":".join(data))
		return lcndb

	def loadRules(self):
		duplicate = {
			"TV": [99000, maxsize],
			"Radio": [99000, maxsize]
		}
		renumbers = {
			"TV": [],
			"Radio": []
		}
		markers = {
			"TV": {},
			"Radio": {}
		}
		rules = config.plugins.LCNScanner.rules.value if config.plugins.LCNScanner.rules.value in self.ruleList.keys() else self.ruleList[0][0]
		dom = self.rulesDom.findall(f".//rules[@name='{rules}']/rule[@type='duplicate']")
		if dom is not None:
			for element in dom:
				modes = self.getModes(element)
				for mode in modes:
					lcnRange = element.get("range", "99000-99999")
					rangeMsg = "starting with 99000"
					markerMsg = ""
					try:
						duplicate[mode] = [int(x) for x in lcnRange.split("-", 1)]
						if len(duplicate[mode]) == 1:
							duplicate[mode].append(maxsize)
							rangeMsg = f"starting with {duplicate[mode][0]}"
						else:
							rangeMsg = f"within the range {duplicate[mode][0]} to {duplicate[mode][1]}"
						marker = element.text
						if marker:
							markers[mode][duplicate[mode][0]] = marker
							markerMsg = f" with a preceding marker of '{marker}'"
					except ValueError as err:
						print(f"[LCNScanner] Error: Duplicate range '{lcnRange}' is invalid!  ({err})")
					print(f"[LCNScanner] Duplicated LCNs for {mode} will be allocated new numbers {rangeMsg}{markerMsg}.")
		dom = self.rulesDom.findall(f".//rules[@name='{rules}']/rule[@type='renumber']")
		if dom is not None:
			for element in dom:
				modes = self.getModes(element)
				for mode in modes:
					lcnRange = element.get("range")
					try:
						lcnRange = [int(x) for x in lcnRange.split("-", 1)]
						if len(lcnRange) != 2:
							raise ValueError("Range format is a pair of numbers separated by a hyphen: <LOWER_LIMIT>-<HIGHER_LIMIT>")
						renumbers[mode].append((lcnRange, element.text))
						print(f"[LCNScanner] LCNs for {mode} in the range {lcnRange[0]} to {lcnRange[1]} will be renumbered with the formula '{element.text}'.")
					except ValueError as err:
						print(f"[LCNScanner] Error: Renumber range '{lcnRange}' is invalid!  ({err})")
		dom = self.rulesDom.findall(f".//rules[@name='{rules}']/rule[@type='marker']")
		if dom is not None:
			for element in dom:
				modes = self.getModes(element)
				for mode in modes:
					lcn = element.get("position")
					if lcn:
						try:
							lcn = int(lcn)
							markers[mode][lcn] = element.text
							print(f"[LCNScanner] Marker '{element.text}' will be added before {mode} LCN {lcn}.")
						except ValueError as err:
							print(f"[LCNScanner] Error: Invalid marker LCN '{lcn}' specified!  ({err})")
		return duplicate, renumbers, markers

	def prepareScan(self):
		# Phase A/B: load, parse and group the LCN entries and work out, for each
		# duplicate group, what the default resolution would be (strongest signal
		# wins, exactly like the previous automatic behaviour). Returns the list
		# of conflicts so the caller can decide whether to ask the user, without
		# writing anything yet.
		print("[LCNScanner] LCN scan started.")
		duplicate, renumbers, markers = self.loadRules()
		lcndb = self.loadLCNs()
		groupsByMode = {}
		servicesByMode = {}
		conflicts = []
		for mode in ("TV", "Radio"):
			services = self.loadServices(mode)
			servicesByMode[mode] = services
			lcns = self.parseLCNEntries(mode, lcndb, services)
			groups = self.buildConflictGroups(mode, lcns, services)
			groupsByMode[mode] = groups
			_, _, _, modeConflicts = self.resolveConflicts(mode, groups, duplicate, renumbers, {})
			conflicts += modeConflicts
		self.scanState = {
			"duplicate": duplicate,
			"renumbers": renumbers,
			"markers": markers,
			"groupsByMode": groupsByMode,
		}
		return conflicts

	def prepareScanAsync(self, callback):
		# Same as prepareScan(), but loads each mode's services via
		# loadServicesAsync() so the interactive UI (keyScan()) doesn't block the
		# rest of the box for the whole duration of a large scan. callback(conflicts)
		# is invoked once both modes are done, exactly as prepareScan() would return.
		print("[LCNScanner] LCN scan started.")
		duplicate, renumbers, markers = self.loadRules()
		lcndb = self.loadLCNs()
		groupsByMode = {}
		conflicts = []
		modes = ["TV", "Radio"]

		def processNextMode():
			if not modes:
				self.scanState = {
					"duplicate": duplicate,
					"renumbers": renumbers,
					"markers": markers,
					"groupsByMode": groupsByMode,
				}
				callback(conflicts)
				return
			mode = modes.pop(0)

			def onServicesLoaded(services):
				lcns = self.parseLCNEntries(mode, lcndb, services)
				groups = self.buildConflictGroups(mode, lcns, services)
				groupsByMode[mode] = groups
				_, _, _, modeConflicts = self.resolveConflicts(mode, groups, duplicate, renumbers, {})
				conflicts.extend(modeConflicts)
				processNextMode()

			self.loadServicesAsync(mode, onServicesLoaded)

		processNextMode()

	def finishScan(self, choices=None, bouquetTarget=None, callback=None):
		# Phase C: apply the (possibly user-chosen) resolution for every duplicate
		# and write the bouquets and the 'lcndb' file, exactly as the previous
		# single-pass implementation did. bouquetTarget selects whether a new
		# bouquet is created (at the head or tail of the bouquet list) or an
		# existing bouquet's content is overwritten in place.
		if self.scanState is None:
			print("[LCNScanner] Error: finishScan() called without a matching prepareScan()!")
			if callback and callable(callback):
				callback()
			return
		choices = choices or {}
		bouquetTarget = bouquetTarget or {"destination": "new", "position": "tail"}
		duplicate = self.scanState["duplicate"]
		renumbers = self.scanState["renumbers"]
		markers = self.scanState["markers"]
		groupsByMode = self.scanState["groupsByMode"]
		lcns = []
		writtenExisting = set()

		def guardedWriteBouquet(mode, medium, serviceLCNs):
			if bouquetTarget.get("destination") == "existing":
				target = bouquetTarget.get("target")
				if target in writtenExisting:
					print(f"[LCNScanner] Warning: Bouquet '{target}' is being overwritten again within the same scan; only the last write will remain.")
				writtenExisting.add(target)
			self.writeBouquet(mode, medium, serviceLCNs, markers, bouquetTarget)

		for mode in ("TV", "Radio"):
			cableLCNs, satelliteLCNs, terrestrialLCNs, _ = self.resolveConflicts(mode, groupsByMode[mode], duplicate, renumbers, choices, remember=True)
			if cableLCNs or satelliteLCNs or terrestrialLCNs:
				if cableLCNs:
					guardedWriteBouquet(mode, "Cable", cableLCNs)
					lcns += self.buildLCNs(cableLCNs)
				if satelliteLCNs:
					guardedWriteBouquet(mode, "Satellite", satelliteLCNs)
					lcns += self.buildLCNs(satelliteLCNs)
				if terrestrialLCNs:
					guardedWriteBouquet(mode, "Terrestrial", terrestrialLCNs)
					lcns += self.buildLCNs(terrestrialLCNs)
			else:
				print("[LCNScanner] Error: No valid entries found in the LCN database! Run a service scan.")
		if lcns:
			lcns.insert(0, "#VERSION 2")
			if fileWriteLines(join(self.configPath, "lcndb"), lcns, source=MODULE_NAME):
				print("[LCNScanner] The 'lcndb' file has been updated.")
			else:
				print("[LCNScanner] Error: The 'lcndb' file could not be updated!")
			eDVBDB.getInstance().reloadServicelist()
		eDVBDB.getInstance().reloadBouquets()
		self.scanState = None
		print("[LCNScanner] LCN scan finished.")
		if callback and callable(callback):
			callback()

	@staticmethod
	def currentBouquetTarget():
		# The persistent destination/position/name settings from the main setup
		# screen (see setup.xml), shared by the interactive scan (keyScan()) and
		# by lcnScan() below so both honour the same configuration.
		return {
			"destination": config.plugins.LCNScanner.bouquetDestination.value,
			"position": config.plugins.LCNScanner.bouquetPosition.value,
			"target": config.plugins.LCNScanner.bouquetTarget.value,
			"name": config.plugins.LCNScanner.bouquetName.value.strip(),
		}

	def lcnScan(self, callback=None):
		# Non-interactive convenience wrapper used directly by this plugin's own
		# menu (when there is nothing to resolve interactively) and, importantly,
		# by the STOCK Screens/ServiceScan.py: it imports this exact class and
		# calls lcnScan(callback=...) automatically after any channel scan
		# finishes, whenever this plugin is installed. Duplicates are resolved
		# from the remembered choice for each LCN slot (see resolveConflicts()),
		# falling back to strongest-signal only the first time a given slot is
		# ever seen, and the bouquet is written using the same
		# destination/position/name the user configured for the interactive scan.
		self.prepareScan()
		self.finishScan(choices=None, bouquetTarget=self.currentBouquetTarget(), callback=callback)


def hasManagedBouquetInstalled():
	"""True if at least one bouquet this plugin has created or overwritten
	(see MANAGED_BOUQUETS_FILENAME) is still referenced from bouquets.tv or
	bouquets.radio. A plain module-level function - rather than only a method
	on LCNScanner - so another plugin can call it directly, e.g.:

		try:
			from Plugins.SystemPlugins.LCNScanner.plugin import hasManagedBouquetInstalled
			shouldRescan = hasManagedBouquetInstalled()
		except ImportError:
			shouldRescan = False
	"""
	configPath = resolveFilename(SCOPE_CONFIG)
	try:
		with open(join(configPath, MANAGED_BOUQUETS_FILENAME), "r", encoding="utf-8") as f:
			names = json.load(f)
	except (OSError, ValueError):
		return False
	if not isinstance(names, list) or not names:
		return False
	for extension in ("tv", "radio"):
		try:
			with open(join(configPath, f"bouquets.{extension}"), "r", encoding="utf-8", errors="ignore") as f:
				content = f.read()
		except OSError:
			continue
		if any(f"\"{name}\"" in content for name in names):
			return True
	return False


class LCNScannerDuplicates(ConfigListScreen, Screen):
	# Try the current skin's own "Setup" screen definition first (skinName lookup),
	# and if that isn't found, fall back to a literal copy of the built-in Setup
	# screen's own layout (same "config"/"description"/"footnote" widgets and the
	# engine's auto button bar panel) instead of a bespoke fixed-size layout, so this
	# popup matches the rest of the interface either way instead of looking foreign.
	skinName = ["Setup"]
	skin = """
	<screen name="LCNScannerDuplicates" position="center,center" size="560,600" title="Resolve duplicate LCNs">
		<panel name="ButtonBarAuto1Panel"/>
		<widget name="config" position="0,60" size="560,330" transparent="0" enableWrapAround="1" scrollbarMode="showOnDemand"/>
		<widget name="description" position="10,e-180" size="540,75" font="Regular;18" halign="center" valign="top" transparent="0" zPosition="1"/>
		<widget name="footnote" position="10,e-105" size="540,20" zPosition="1" font="Regular;18" halign="left" transparent="1" valign="top"/>
	</screen>"""

	def __init__(self, session, conflicts):
		Screen.__init__(self, session)
		self.setTitle(_("Resolve duplicate LCNs"))
		# self.entries: [(conflict, ConfigSelection), ...] in display order, kept in
		# sync with self["config"].list built below.
		self.entries = []
		configList = []
		for conflict in sorted(conflicts, key=lambda c: (c["mode"], c["medium"], c["lcn"])):
			labels = [candidate[0] for candidate in conflict["candidates"]]
			selection = ConfigSelection(choices=labels, default=labels[conflict["default"]])
			self.entries.append((conflict, selection))
			configList.append(getConfigListEntry(f"{conflict['mode']} / {conflict['medium']} - LCN {conflict['lcn']}", selection))
		ConfigListScreen.__init__(self, configList, session=session)
		# Short labels so they fit the skin's button bar without wrapping onto a
		# second line; the fuller explanation lives in the description line below
		# and in the help text bound to each action.
		self["key_red"] = StaticText(_("Cancel"))
		self["key_green"] = StaticText(_("Save"))
		# The skin's own "Setup" screen (picked up via skinName above) expects these
		# extra sources; without them the skin engine fails to apply it and silently
		# falls back to the plain default skin below, which is what made this popup
		# look inconsistent with the rest of the interface.
		self["footnote"] = StaticText("")
		self["description"] = StaticText(_("Cancel uses the default (strongest signal) resolution for every duplicate."))
		self["key_yellow"] = StaticText("")
		self["key_blue"] = StaticText("")
		self["key_menu"] = StaticText("")
		self["key_info"] = StaticText("")
		self["key_help"] = StaticText("")
		self["actions"] = HelpableActionMap(self, ["OkCancelActions", "ColorActions"], {
			"cancel": (self.keyCancel, _("Discard changes and use the default (strongest signal) resolution")),
			"red": (self.keyCancel, _("Discard changes and use the default (strongest signal) resolution")),
			"save": (self.keySave, _("Save the selected duplicate resolutions")),
			"green": (self.keySave, _("Save the selected duplicate resolutions")),
		}, prio=0, description=_("LCN Scanner Duplicates Actions"))

	def buildResult(self, useDefaults=False):
		result = {}
		for conflict, selection in self.entries:
			labels = [candidate[0] for candidate in conflict["candidates"]]
			index = conflict["default"] if useDefaults else labels.index(selection.value)
			result[(conflict["mode"], conflict["medium"], conflict["lcn"])] = index
		return result

	def keySave(self):
		self.close(self.buildResult(useDefaults=False))

	def keyCancel(self):
		self.close(self.buildResult(useDefaults=True))


class LCNScannerSetup(LCNScanner, Setup):
	# Screens.Setup translates each setup.xml item's "text" through OpenATV's own
	# translation catalogue, which only covers the items this plugin shipped with
	# upstream (LCN rules, spacer lines, etc) — not the bouquet destination/position/
	# name items added here. createSetup() below is called every time the list is
	# (re)built, including when "Bouquet destination" changes and items are shown or
	# hidden, so patching the labels there (via this file's own _(), which the .po/.mo
	# under locale/ does cover) keeps them translated consistently instead of only
	# some of the list being in Italian and the rest in English.
	UNTRANSLATED_ITEM_TEXT = ("Bouquet destination", "Bouquet position", "Bouquet to overwrite", "Bouquet name (empty = automatic)")

	def __init__(self, session):
		LCNScanner.__init__(self)
		Setup.__init__(self, session=session, setup="LCNScanner", plugin="SystemPlugins/LCNScanner")
		self["scanActions"] = HelpableActionMap(self, "ColorActions", {
			"yellow": (self.keyScan, _("Scan for terrestrial LCNs and create LCN bouquets"))
		}, prio=0, description=_("LCN Scanner Actions"))
		lines = fileReadLines(resolveFilename(SCOPE_CONFIG, "lcndb"), default=[], source=MODULE_NAME)
		if len(lines) > 1:
			self["scanActions"].setEnabled(True)
			self["key_yellow"] = StaticText(_("Scan"))
		else:
			self["scanActions"].setEnabled(False)
			self["key_yellow"] = StaticText("")

	def createSetup(self):
		Setup.createSetup(self)
		patched = []
		changed = False
		for entry in self.list:
			if entry and entry[0] in self.UNTRANSLATED_ITEM_TEXT:
				entry = (_(entry[0]),) + tuple(entry[1:])
				changed = True
			patched.append(entry)
		if changed:
			self.list = patched
			self["config"].list = self.list

	def keyScan(self):
		def performScan():
			def resetControls():
				self["scanActions"].setEnabled(True)
				self["key_yellow"].setText(_("Scan"))
				self.setFootnote("")

			def keyScanCallback():
				self.timer = eTimer()
				self.timer.callback.append(resetControls)
				self.timer.startLongTimer(2)

			def askBouquetTarget(choices):
				# Destination/position/name are now persistent settings on the main
				# setup screen (see setup.xml) rather than a popup shown after every
				# scan, so they only need to be read here.
				bouquetTarget = self.currentBouquetTarget()
				if bouquetTarget["destination"] == "existing" and not bouquetTarget["target"]:
					resetControls()
					self.session.open(MessageBox, _("No existing bouquets were found to overwrite."), MessageBox.TYPE_ERROR, timeout=5)
					return
				self["scanActions"].setEnabled(False)
				self["key_yellow"].setText("")
				self.setFootnote(_("Please wait while LCN bouquets are created/updated..."))
				self.finishScan(choices=choices, bouquetTarget=bouquetTarget, callback=keyScanCallback)

			def onDuplicatesResolved(choices):
				askBouquetTarget(choices)

			def onPrepared(conflicts):
				if conflicts:
					resetControls()
					self.session.openWithCallback(onDuplicatesResolved, LCNScannerDuplicates, conflicts)
				else:
					askBouquetTarget({})

			self.prepareScanAsync(onPrepared)

		self["scanActions"].setEnabled(False)
		self["key_yellow"].setText("")
		self.setFootnote(_("Please wait while the LCN database is being analysed..."))
		self.timer = eTimer()
		self.timer.callback.append(performScan)
		self.timer.start(0, True)  # Yield to the idle loop to allow a screen update.

	def keySave(self):
		if hasattr(self, "timer"):
			self.timer.stop()
		Setup.keySave(self)


def main(session, **kwargs):
	session.open(LCNScannerSetup)


def menu(menuid, **kwargs):
	return [("LCN Scanner", main, "LCNScanner", None)] if menuid == "scan" else []


def Plugins(**kwargs):
	pluginList = []
	description = _("LCN Scanner plugin for DVB-C/T/T2 services")
	pluginList.append(PluginDescriptor(where=[PluginDescriptor.WHERE_MENU], description=description, needsRestart=False, fnc=menu))
	if config.plugins.LCNScanner.showInPluginsList.value:
		pluginList.append(PluginDescriptor(name=_("LCN Scanner"), where=[PluginDescriptor.WHERE_PLUGINMENU], description=description, icon="LCNScanner.png", fnc=main))
	return pluginList
