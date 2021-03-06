import abc
import time
import asyncio
import logging
import weakref
import functools
import itertools
from urllib import parse

import furl
import aiohttp

from waterbutler import settings
from waterbutler.core import streams
from waterbutler.core import exceptions
from waterbutler.core.utils import ZipStreamGenerator
from waterbutler.core.utils import RequestHandlerContext

logger = logging.getLogger(__name__)
_THROTTLES = weakref.WeakKeyDictionary()


def throttle(concurrency=10, interval=1):
    def _throttle(func):
        @functools.wraps(func)
        async def wrapped(*args, **kwargs):
            if asyncio.get_event_loop() not in _THROTTLES:
                count, last_call, event = 0, time.time(), asyncio.Event()
                _THROTTLES[asyncio.get_event_loop()] = (count, last_call, event)
                event.set()
            else:
                count, last_call, event = _THROTTLES[asyncio.get_event_loop()]

            await event.wait()
            count += 1
            if count > concurrency:
                count = 0
                if (time.time() - last_call) < interval:
                    event.clear()
                    await asyncio.sleep(interval - (time.time() - last_call))
                    event.set()

            last_call = time.time()
            return (await func(*args, **kwargs))
        return wrapped
    return _throttle


def build_url(base, *segments, **query):
    url = furl.furl(base)
    # Filters return generators
    # Cast to list to force "spin" it
    url.path.segments = list(filter(
        lambda segment: segment,
        map(
            # Furl requires everything to be quoted or not, no mixtures allowed
            # prequote everything so %signs don't break everything
            lambda segment: parse.quote(segment.strip('/')),
            # Include any segments of the original url, effectively list+list but returns a generator
            itertools.chain(url.path.segments, segments)
        )
    ))
    url.args = query
    return url.url


class BaseProvider(metaclass=abc.ABCMeta):
    """The base class for all providers. Every provider must, at the least, implement all abstract
    methods in this class.

    .. note::
        When adding a new provider you must add it to setup.py's
        `entry_points` under the `waterbutler.providers` key formatted
        as: `<provider name> = waterbutler.providers.yourprovider:<FullProviderName>`

        Keep in mind that `yourprovider` modules must export the provider class
    """

    BASE_URL = None

    def __init__(self, auth, credentials, settings, retry_on={408, 502, 503, 504}):
        """
        :param dict auth: Information about the user this provider will act on the behalf of
        :param dict credentials: The credentials used to authenticate with the provider,
            ofter an OAuth 2 token
        :param dict settings: Configuration settings for this provider,
            often folder or repo
        """
        self._retry_on = retry_on
        self.auth = auth
        self.credentials = credentials
        self.settings = settings

    @abc.abstractproperty
    def NAME(self):
        raise NotImplementedError

    def __eq__(self, other):
        try:
            return (
                type(self) == type(other) and
                self.credentials == other.credentials
            )
        except AttributeError:
            return False

    def serialized(self):
        return {
            'name': self.NAME,
            'auth': self.auth,
            'settings': self.settings,
            'credentials': self.credentials,
        }

    def build_url(self, *segments, **query):
        """A nice wrapper around furl, builds urls based on self.BASE_URL

        :param tuple \*segments: A tuple of strings joined into /foo/bar/..
        :param dict \*\*query: A dictionary that will be turned into query parameters ?foo=bar
        :rtype: str
        """
        return build_url(self.BASE_URL, *segments, **query)

    @property
    def default_headers(self):
        """Headers to be included with every request
        Commonly OAuth headers or Content-Type
        """
        return {}

    def build_headers(self, **kwargs):
        headers = self.default_headers
        headers.update(kwargs)
        return {
            key: value
            for key, value in headers.items()
            if value is not None
        }

    @throttle()
    async def make_request(self, method, url, *args, **kwargs):
        """A wrapper around :func:`aiohttp.request`. Inserts default headers.

        :param str method: The HTTP method
        :param str url: The url to send the request to
        :keyword range: An optional tuple (start, end) that is transformed into a Range header
        :keyword expects: An optional tuple of HTTP status codes as integers raises an exception
            if the returned status code is not in it.
        :type expects: tuple of ints
        :param Exception throws: The exception to be raised from expects
        :param tuple \*args: args passed to :func:`aiohttp.request`
        :param dict \*\*kwargs: kwargs passed to :func:`aiohttp.request`
        :rtype: :class:`aiohttp.Response`
        :raises ProviderError: Raised if expects is defined
        """
        kwargs['headers'] = self.build_headers(**kwargs.get('headers', {}))
        retry = _retry = kwargs.pop('retry', 2)
        range = kwargs.pop('range', None)
        expects = kwargs.pop('expects', None)
        throws = kwargs.pop('throws', exceptions.ProviderError)
        if range:
            kwargs['headers']['Range'] = self._build_range_header(range)

        if callable(url):
            url = url()
        while retry >= 0:
            try:
                response = await aiohttp.request(method, url, *args, **kwargs)
                if expects and response.status not in expects:
                    raise (await exceptions.exception_from_response(response, error=throws, **kwargs))
                return response
            except throws as e:
                if retry <= 0 or e.code not in self._retry_on:
                    raise
                await asyncio.sleep((1 + _retry - retry) * 2)
                retry -= 1

    def request(self, *args, **kwargs):
        return RequestHandlerContext(self.make_request(*args, **kwargs))

    async def move(self, dest_provider, src_path, dest_path, rename=None, conflict='replace', handle_naming=True):
        """Moves a file or folder from the current provider to the specified one
        Performs a copy and then a delete.
        Calls :func:`BaseProvider.intra_move` if possible.

        :param BaseProvider dest_provider: The provider to move to
        :param dict source_options: A dict to be sent to either :func:`BaseProvider.intra_move`
            or :func:`BaseProvider.copy` and :func:`BaseProvider.delete`
        :param dict dest_options: A dict to be sent to either :func:`BaseProvider.intra_move`
            or :func:`BaseProvider.copy`
        """
        args = (dest_provider, src_path, dest_path)
        kwargs = {'rename': rename, 'conflict': conflict}

        if handle_naming:
            dest_path = await dest_provider.handle_naming(
                src_path,
                dest_path,
                rename=rename,
                conflict=conflict,
            )
            args = (dest_provider, src_path, dest_path)
            kwargs = {}

        if self.can_intra_move(dest_provider, src_path):
            return (await self.intra_move(*args))

        if src_path.is_dir:
            metadata, created = await self._folder_file_op(self.move, *args, **kwargs)
        else:
            metadata, created = await self.copy(*args, handle_naming=False, **kwargs)

        await self.delete(src_path)

        return metadata, created

    async def copy(self, dest_provider, src_path, dest_path, rename=None, conflict='replace', handle_naming=True):
        args = (dest_provider, src_path, dest_path)
        kwargs = {'rename': rename, 'conflict': conflict, 'handle_naming': handle_naming}

        if handle_naming:
            dest_path = await dest_provider.handle_naming(
                src_path,
                dest_path,
                rename=rename,
                conflict=conflict,
            )
            args = (dest_provider, src_path, dest_path)
            kwargs = {}

        if self.can_intra_copy(dest_provider, src_path):
                return (await self.intra_copy(*args))

        if src_path.is_dir:
            return (await self._folder_file_op(self.copy, *args, **kwargs))

        download_stream = await self.download(src_path)

        if getattr(download_stream, 'name', None):
            dest_path.rename(download_stream.name)

        return (await dest_provider.upload(download_stream, dest_path))

    async def _folder_file_op(self, func, dest_provider, src_path, dest_path, **kwargs):
        """Recursively apply func to src/dest path.

        Called from: func: copy and move if src_path.is_dir.

        Calls: func: dest_provider.delete and notes result for bool: created
               func: dest_provider.create_folder
               func: dest_provider.revalidate_path
               func: self.metadata

        :param coroutine func: to be applied to src/dest path
        :param *Provider dest_provider: Destination provider
        :param *ProviderPath src_path: Source path
        :param *ProviderPath dest_path: Destination path
        """
        assert src_path.is_dir, 'src_path must be a directory'
        assert asyncio.iscoroutinefunction(func), 'func must be a coroutine'

        try:
            await dest_provider.delete(dest_path)
            created = False
        except exceptions.ProviderError as e:
            if e.code != 404:
                raise
            created = True

        folder = await dest_provider.create_folder(dest_path, folder_precheck=False)

        dest_path = await dest_provider.revalidate_path(dest_path.parent, dest_path.name, folder=dest_path.is_dir)

        folder.children = []
        items = await self.metadata(src_path)

        for i in range(0, len(items), settings.OP_CONCURRENCY):
            futures = []
            for item in items[i:i + settings.OP_CONCURRENCY]:
                futures.append(asyncio.ensure_future(
                    func(
                        dest_provider,
                        # TODO figure out a way to cut down on all the requests made here
                        (await self.revalidate_path(src_path, item.name, folder=item.is_folder)),
                        (await dest_provider.revalidate_path(dest_path, item.name, folder=item.is_folder)),
                        handle_naming=False,
                    )
                ))

                if item.is_folder:
                    await futures[-1]

            if not futures:
                continue

            done, _ = await asyncio.wait(futures, return_when=asyncio.FIRST_EXCEPTION)

            for fut in done:
                folder.children.append(fut.result()[0])

        return folder, created

    async def handle_naming(self, src_path, dest_path, rename=None, conflict='replace'):
        """Given a WaterButlerPath and the desired name, handle any potential naming issues.

        i.e.:
            cp /file.txt /folder/ -> /folder/file.txt
            cp /folder/ /folder/ -> /folder/folder/
            cp /file.txt /folder/file.txt -> /folder/file.txt
            cp /file.txt /folder/file.txt -> /folder/file (1).txt
            cp /file.txt /folder/doc.txt -> /folder/doc.txt

        :param WaterButlerPath src_path: The object that is being copied
        :param WaterButlerPath dest_path: The path that is being copied to or into
        :param str rename: The desired name of the resulting path, may be incremented
        :param str conflict: The conflict resolution strategy, replace or keep

        Returns: WaterButlerPath dest_path: The path of the desired result.
        """
        if src_path.is_dir and dest_path.is_file:
            # Cant copy a directory to a file
            raise ValueError('Destination must be a directory if the source is')

        if not dest_path.is_file:
            # Directories always are going to be copied into
            # cp /folder1/ /folder2/ -> /folder1/folder2/
            dest_path = await self.revalidate_path(
                dest_path,
                rename or src_path.name,
                folder=src_path.is_dir
            )

        dest_path, _ = await self.handle_name_conflict(dest_path, conflict=conflict)

        return dest_path

    def can_intra_copy(self, other, path=None):
        """Indicates if a quick copy can be performed between the current provider and `other`.

        .. note::
            Defaults to False

        :param waterbutler.core.provider.BaseProvider other: The provider to check against
        :rtype: bool
        """
        return False

    def can_intra_move(self, other, path=None):
        """Indicates if a quick move can be performed between the current provider and `other`.

        .. note::
            Defaults to False

        :param waterbutler.core.provider.BaseProvider other: The provider to check against
        :rtype: bool
        """
        return False

    def intra_copy(self, dest_provider, source_options, dest_options):
        raise NotImplementedError

    async def intra_move(self, dest_provider, src_path, dest_path):
        data, created = await self.intra_copy(dest_provider, src_path, dest_path)
        await self.delete(src_path)
        return data, created

    async def exists(self, path, **kwargs):
        """Check for existence of WaterButlerPath

        Attempt to retrieve provider metadata to determine existence of a WaterButlerPath.  If
        successful, will return the result of `self.metadata()` which may be `[]` for empty
        folders.

        :param WaterButlerPath path: path to check for
        :rtype: (`self.metadata()` or False)
        """
        try:
            return (await self.metadata(path, **kwargs))
        except exceptions.NotFoundError:
            return False
        except exceptions.MetadataError as e:
            if e.code != 404:
                raise
        return False

    async def handle_name_conflict(self, path, conflict='replace', **kwargs):
        """Check WaterButlerPath and resolve conflicts

        Given a WaterButlerPath and a conflict resolution pattern determine
        the correct file path to upload to and indicate if that file exists or not

        :param WaterButlerPath path: Desired path to check for conflict
        :param str conflict: replace, keep, warn
        :rtype: (WaterButlerPath, provider.metadata() or False)
        :raises: NamingConflict
        """
        exists = await self.exists(path, **kwargs)
        if (not exists and not exists == []) or conflict == 'replace':
            return path, exists
        if conflict == 'warn':
            raise exceptions.NamingConflict(path)

        while True:
            path.increment_name()
            test_path = await self.revalidate_path(
                path.parent,
                path.name,
                folder=path.is_dir
            )

            exists = await self.exists(test_path, **kwargs)
            if not (exists or exists == []):
                break

        return path, False

    async def revalidate_path(self, base, path, folder=False):
        """Take a path and a base path and build a WaterButlerPath representing `/base/path`.  For
        id-based providers, this will need to lookup the id of the new child object.

        :param WaterButlerPath base: The base folder to look under
        :param str path: the path of a child of `base`, relative to `base`
        :param bool folder: whether the returned WaterButlerPath should represent a folder
        :rtype: WaterButlerPath
        """
        return base.child(path, folder=folder)

    async def zip(self, path, **kwargs):
        """Streams a Zip archive of the given folder

        :param str path: The folder to compress
        """

        metadata = await self.metadata(path)
        if path.is_file:
            metadata = [metadata]
            path = path.parent

        return streams.ZipStreamReader(ZipStreamGenerator(self, path, *metadata))

    @abc.abstractmethod
    def can_duplicate_names(self):
        """Returns True if a file and a folder in the same directory can have identical names."""
        raise NotImplementedError

    @abc.abstractmethod
    def download(self, **kwargs):
        """Download a file from this provider.

        :param dict \*\*kwargs: Arguments to be parsed by child classes
        :rtype: :class:`waterbutler.core.streams.ResponseStreamReader`
        :raises: :class:`waterbutler.core.exceptions.DownloadError`
        """
        raise NotImplementedError

    @abc.abstractmethod
    def upload(self, stream, **kwargs):
        """

        :param dict \*\*kwargs: Arguments to be parsed by child classes
        :rtype: (:class:`waterbutler.core.metadata.BaseFileMetadata`, :class:`bool`)
        :raises: :class:`waterbutler.core.exceptions.DeleteError`
        """
        raise NotImplementedError

    @abc.abstractmethod
    def delete(self, **kwargs):
        """

        :param dict \*\*kwargs: Arguments to be parsed by child classes
        :rtype: :class:`None`
        :raises: :class:`waterbutler.core.exceptions.DeleteError`
        """
        raise NotImplementedError

    @abc.abstractmethod
    def metadata(self, **kwargs):
        """Get metdata about the specified resource from this provider. Will be a :class:`list`
        if the resource is a directory otherwise an instance of
        :class:`waterbutler.core.metadata.BaseFileMetadata`

        :param dict \*\*kwargs: Arguments to be parsed by child classes
        :rtype: :class:`waterbutler.core.metadata.BaseMetadata`
        :rtype: :class:`list` of :class:`waterbutler.core.metadata.BaseMetadata`
        :raises: :class:`waterbutler.core.exceptions.MetadataError`
        """
        raise NotImplementedError

    @abc.abstractmethod
    def validate_v1_path(self, path, **kwargs):
        """API v1 requires that requests against folder endpoints always end with a slash, and
        requests against files never end with a slash.  This method checks the provider's metadata
        for the given id and throws a 404 Not Found if the implicit and explicit types don't
        match.  This method duplicates the logic in the provider's validate_path method, but
        validate_path must currently accomodate v0 AND v1 semantics.  After v0's retirement, this
        method can replace validate_path.

        :param str path: user-supplied path to validate
        :rtype: :class:`waterbutler.core.path`
        :raises: :class:`waterbutler.core.exceptions.NotFoundError`

        """
        raise NotImplementedError

    @abc.abstractmethod
    def validate_path(self, path, **kwargs):
        raise NotImplementedError

    def path_from_metadata(self, parent_path, metadata):
        return parent_path.child(metadata.name, _id=metadata.path.strip('/'), folder=metadata.is_folder)

    def revisions(self, **kwargs):
        return []  # TODO Raise 405 by default h/t @rliebz

    def create_folder(self, path, **kwargs):
        """Create a folder in the current provider at `path`. Returns a `BaseFolderMetadata` object
        if successful.  May throw a 409 Conflict if a directory with the same name already exists.

        :param str path: user-supplied path to create. must be a directory.
        :param boolean precheck_folder: flag to check for folder before attempting create
        :rtype: :class:`waterbutler.core.metadata.BaseFolderMetadata`
        :raises: :class:`waterbutler.core.exceptions.FolderCreationError`
        """
        raise exceptions.ProviderError({'message': 'Folder creation not supported.'}, code=405)

    def _build_range_header(self, slice_tup):
        start, end = slice_tup
        return 'bytes={}-{}'.format(
            '' if start is None else start,
            '' if end is None else end
        )

    def __repr__(self):
        # Note: credentials are not included on purpose.
        return ('<{}({}, {})>'.format(self.__class__.__name__, self.auth, self.settings))
