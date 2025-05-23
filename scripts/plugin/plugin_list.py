import asyncio
import os
import shutil
import time
from traceback import format_exc
from typing import Callable, List, Collection, Optional, Coroutine, Set

from common import constants, log
from common.report import reporter
from meta.all import PluginMetaSummary, Everything
from meta.author import AuthorSummary
from plugin.plugin import Plugin
from utils import file_utils


class PluginList(List[Plugin]):
	def __init__(self):
		super().__init__()
		self.__inited = False
		self.__fetched_stuffs: Set[str] = set()

	def init(self, target_ids: Optional[Collection[str]]):
		if self.__inited:
			return
		self.clear()
		for folder in os.listdir(constants.PLUGINS_FOLDER):
			if (constants.PLUGINS_FOLDER / folder).is_dir():
				if target_ids is None or folder in target_ids:
					log.info('Found plugin {}'.format(folder))
					try:
						plugin = Plugin(folder)
					except Exception as e:
						log.exception('Failed to initialize plugin in folder "{}":\n {}'.format(folder, format_exc()))
						reporter.record_plugin_failure(folder, 'Initialize plugin in folder {} failed'.format(folder), e)
						continue
					else:
						if plugin.is_disabled():
							log.info('Plugin {} is disabled due to "{}"'.format(plugin, plugin.get_disable_reason()))
							reporter.record_plugin_disabled(plugin.id, plugin.get_disable_reason())
						else:
							self.append(plugin)
				else:
					log.debug('Skipping plugin {}'.format(folder))

		log.info('Found {} plugins in total'.format(len(self)))
		self.sort(key=lambda plg: plg.id.lower())
		self.__inited = True

	async def __fetch(self, fetch_target_name: str, func: Callable[[Plugin], Coroutine], fail_hard: bool):
		if fetch_target_name in self.__fetched_stuffs:
			return

		async def task_wrapper(plg: Plugin):
			try:
				await func(plg)
			except Exception as e:
				log.error('Failed to fetch {} of plugin {}'.format(fetch_target_name, plg))
				reporter.record_plugin_failure(plg.id, 'Fetch {} failed'.format(fetch_target_name), e)
				if fail_hard:
					log.error('Fail-HARD!')
					raise
				else:
					log.exception('{}: {}'.format(type(e), e))

		async with asyncio.TaskGroup() as tg:
			for plugin in self:
				tg.create_task(task_wrapper(plugin))

		self.__fetched_stuffs.add(fetch_target_name)

	async def fetch_data(self, *, fail_hard: bool, skip_release: bool = False, reuse_old_on_failures: bool = False):
		log.info('Fetching data')

		# fetch repos first, maybe the repos var needs some update (e.g. repos rename)
		await self.__fetch('repository', lambda plg: plg.fetch_and_update_repository(), fail_hard=fail_hard)
		async with asyncio.TaskGroup() as tg:
			tg.create_task(self.__fetch('introduction', lambda plg: plg.fetch_introduction(), fail_hard=fail_hard))
			tg.create_task(self.__fetch('meta', lambda plg: plg.fetch_meta(), fail_hard=fail_hard))
			if not skip_release:
				tg.create_task(self.__fetch('release', lambda plg: plg.fetch_release(), fail_hard=fail_hard))

		if reuse_old_on_failures:
			self.reuse_old_data_for_plugin_failures()

	def reuse_old_data_for_plugin_failures(self):
		log.info('Loading and reusing data for fetching failures')
		for plugin in self:
			try:
				plugin.load_old_request_meta()
				plugin.load_old_introduction()
				plugin.load_old_meta_info()
				plugin.load_old_release_summary()
				plugin.load_old_repository_info()
				plugin.reuse_old_fetch_results()
			except Exception as e:
				log.exception('Loading and reusing data for plugin {} failed'.format(plugin))
				reporter.record_plugin_failure(plugin.id, 'Reuse plugin info', e)

	def store_data(self):
		log.info('Storing data into meta folder')

		# prepare folder
		if constants.META_FOLDER.is_dir():
			# raise possible directory operation error before cleaning the meta folder content
			old_dir = constants.META_FOLDER.with_name(constants.META_FOLDER.name + '.old')
			os.rename(constants.META_FOLDER, old_dir)
			shutil.rmtree(old_dir)
		os.makedirs(constants.META_FOLDER)

		# make readme
		with file_utils.open_for_read(constants.TEMPLATE_FOLDER / 'meta_readme.md') as f:
			readme: str = f.read()
		placeholders = {
			'"##PLUGIN_INFO_SCHEMA_VERSION##"': constants.PLUGIN_INFO_SCHEMA_VERSION,
			'"##META_INFO_SCHEMA_VERSION##"': constants.META_INFO_SCHEMA_VERSION,
			'"##RELEASE_INFO_SCHEMA_VERSION##"': constants.RELEASE_INFO_SCHEMA_VERSION,
		}
		for key, value in placeholders.items():
			readme = readme.replace(str(key), str(value))
		with file_utils.open_for_write(constants.META_FOLDER / 'readme.md') as f:
			f.write(readme)

		# store info for each plugin
		for plugin in self:
			try:
				plugin.save_request_cache()
				plugin.save_meta_info_if_available()
				plugin.save_release_summary_if_available()
				plugin.save_formatted_plugin_info()
				plugin.save_repository_info_if_available()
				plugin.save_request_meta()
			except Exception as e:
				log.exception('Storing info for plugin {}'.format(plugin))
				reporter.record_plugin_failure(plugin.id, 'Store plugin info', e)

		# make and store plugin summary
		meta_summary = PluginMetaSummary(
			plugin_amount=len(self),
			plugins={},
			plugin_info={},
		)
		for plugin in self:
			meta_summary.plugins[plugin.id] = plugin.meta_info
			meta_summary.plugin_info[plugin.id] = plugin.generate_formatted_plugin_info()
		file_utils.save_json(meta_summary.serialize(), constants.META_FOLDER / 'plugins.json', compact=True, with_gz=True)

		# make and store author summary
		author_summary = AuthorSummary()
		for plugin in self:
			for author in plugin.authors:
				author_summary.add_author(author.model_copy(), plugin.id)
		author_summary.finalize()
		file_utils.save_json(author_summary.serialize(), constants.META_FOLDER / 'authors.json', with_gz=True)

		# everything
		everything = Everything(
			timestamp=int(time.time()),
			authors=author_summary,
			plugins={},
		)
		for plugin in self:
			aop = plugin.create_and_save_all_data()
			everything.plugins[plugin.id] = aop
		file_utils.save_json(everything.serialize(), constants.META_FOLDER / 'everything.json', compact=True, with_gz=True, with_xz=True)

		# everything (slim)
		for p in everything.plugins.values():
			p.plugin.introduction = {}
			p.plugin.introduction_urls = {}
			if p.repository is not None:
				p.repository.readme = None
			if p.release is not None:
				for r in p.release.releases:
					r.description = None
		file_utils.save_json(everything.serialize(), constants.META_FOLDER / 'everything_slim.json', compact=True, with_gz=True, with_xz=True)

		log.info('Stored data into meta folder')


_plugin_list = PluginList()


def get_plugin_list(target_ids: Optional[Collection[str]] = None) -> PluginList:
	_plugin_list.init(target_ids)
	return _plugin_list
