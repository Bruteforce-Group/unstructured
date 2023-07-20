from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import TYPE_CHECKING, List
from urllib.parse import urlparse

from unstructured.file_utils.filetype import EXT_TO_FILETYPE
from unstructured.ingest.interfaces import (
    BaseConnector,
    BaseConnectorConfig,
    BaseIngestDoc,
    ConnectorCleanupMixin,
    IngestDocCleanupMixin,
    StandardConnectorConfig,
)
from unstructured.ingest.logger import logger
from unstructured.utils import requires_dependencies

if TYPE_CHECKING:
    from office365.sharepoint.files.file import File

MAX_MB_SIZE = 512_000_000


@dataclass
class SimpleSharepointConfig(BaseConnectorConfig):
    client_id: str
    client_credential: str = field(repr=False)
    site_url: str
    path: str
    process_all: bool = False
    process_pages: bool = False
    recursive: bool = False

    def __post_init__(self):
        if not (self.client_id and self.client_credential and self.site_url):
            raise ValueError(
                "Please provide one of the following mandatory values:"
                "\n-ms-client_id\n-ms-client_cred\n-ms-sharepoint-site",
            )


@dataclass
class SharepointIngestDoc(IngestDocCleanupMixin, BaseIngestDoc):
    config: SimpleSharepointConfig
    file: "File"
    meta: dict

    def __post_init__(self):
        self.ext = "".join(Path(self.file.name).suffixes) if self.meta is None else ".html"
        if not self.ext:
            raise ValueError("Unsupported file without extension.")

        if self.ext not in EXT_TO_FILETYPE.keys():
            raise ValueError(
                f"Extension not supported. "
                f"Value MUST be one of {', '.join([k for k in EXT_TO_FILETYPE if k is not None])}.",
            )
        self._set_download_paths()

    def _set_download_paths(self) -> None:
        """Parses the folder structure from the source and creates the download and output paths"""
        download_path = Path(f"{self.standard_config.download_dir}")
        output_path = Path(f"{self.standard_config.output_dir}")
        if self.meta is not None:
            parent = (
                Path(self.meta["url"]).with_suffix(self.ext)
                if (self.meta["site_path"] is None)
                else Path(self.meta["site_path"] + "/" + self.meta["url"]).with_suffix(self.ext)
            )
        else:
            parent = Path(self.file.serverRelativeUrl[1:])
        self.download_dir = (download_path / parent.parent).resolve()
        self.download_filepath = (download_path / parent).resolve()
        oname = f"{str(parent)[:-len(self.ext)]}.json"
        self.output_dir = (output_path / parent.parent).resolve()
        self.output_filepath = (output_path / oname).resolve()

    @property
    def filename(self):
        return Path(self.download_filepath).resolve()

    @property
    def _output_filename(self):
        return Path(self.output_filepath).resolve()

    def _get_page(self):
        """Retrieves HTML content of the Sharepoint site through the CanvasContent1 and
        LayoutWebpartsContent1"""

        try:
            content_labels = ["CanvasContent1", "LayoutWebpartsContent1"]
            content = self.file.listItemAllFields.select(content_labels).get().execute_query()
            pld = (content.properties.get("LayoutWebpartsContent1", "") or "") + (
                content.properties.get("CanvasContent1", "") or ""
            )
            if pld != "":
                pld = unescape(pld)
            else:
                logger.info(
                    f"Page {self.meta['url']} as it has no retrievable content. Dumping empty doc.",
                )
                pld = "<div></div>"

            self.output_dir.mkdir(parents=True, exist_ok=True)
            if not self.download_dir.is_dir():
                logger.debug(f"Creating directory: {self.download_dir}")
                self.download_dir.mkdir(parents=True, exist_ok=True)
            with self.filename.open(mode="w") as f:
                f.write(pld)
        except Exception as e:
            logger.error(f"Error while downloading and saving file: {self.filename}.")
            logger.error(e)
            return
        logger.info(f"File downloaded: {self.filename}")

    def _get_file(self):
        try:
            fsize = self.file.get_property("size", 0)
            self.output_dir.mkdir(parents=True, exist_ok=True)

            if not self.download_dir.is_dir():
                logger.debug(f"Creating directory: {self.download_dir}")
                self.download_dir.mkdir(parents=True, exist_ok=True)

            if fsize > MAX_MB_SIZE:
                logger.info(f"Downloading file with size: {fsize} bytes in chunks")
                with self.filename.open(mode="wb") as f:
                    self.file.download_session(f, chunk_size=1024 * 1024 * 100).execute_query()
            else:
                with self.filename.open(mode="wb") as f:
                    self.file.download(f).execute_query()
        except Exception as e:
            logger.error(f"Error while downloading and saving file: {self.filename}.")
            logger.error(e)
            return
        logger.info(f"File downloaded: {self.filename}")

    @BaseIngestDoc.skip_if_file_exists
    @requires_dependencies(["office365"])
    def get_file(self):
        if self.meta is None:
            self._get_file()
        else:
            self._get_page()
        return


class SharepointConnector(ConnectorCleanupMixin, BaseConnector):
    config: SimpleSharepointConfig
    tenant: None

    def __init__(self, standard_config: StandardConnectorConfig, config: SimpleSharepointConfig):
        super().__init__(standard_config, config)
        self._setup_client()

    @requires_dependencies(["office365"])
    def _setup_client(self):
        from office365.runtime.auth.client_credential import ClientCredential
        from office365.sharepoint.client_context import ClientContext

        if self.config.process_all:
            parsed_url = urlparse(self.config.site_url)
            site_hostname = (parsed_url.hostname or "").split(".")
            tenant_url = site_hostname[0].split("-")
            if tenant_url[-1] != "admin" or (
                parsed_url.path is not None and parsed_url.path != "/"
            ):
                raise ValueError(
                    "A site url in the form of https://[tenant]-admin.sharepoint.com \
                        is required to process all sites within a tenant.",
                )
            self.base_site_url = parsed_url._replace(
                netloc=parsed_url.netloc.replace(site_hostname[0], tenant_url[0]),
            ).geturl()

        self.client = ClientContext(self.config.site_url).with_credentials(
            ClientCredential(self.config.client_id, self.config.client_credential),
        )

    def _list_files(self, root, recursive) -> List["File"]:
        folder = root.expand(["Files", "Folders"]).get().execute_query()
        files = list(folder.files)
        if not recursive:
            return files
        for f in folder.folders:
            files += self._list_objects(f, recursive)
        return files

    def _list_pages(self, site_client) -> list:
        pages = site_client.site_pages.pages.get().execute_query()
        pfiles = []

        for page_meta in pages:
            page_url = page_meta.get_property("Url", None)
            if page_url is None:
                logger.info("Missing site_url. Omitting page... ")
                break
            page_url = f"/{page_url}" if page_url[0] != "/" else page_url
            file_page = site_client.web.get_file_by_server_relative_path(page_url)
            site_path = None
            if (spath := (urlparse(site_client.base_url).path)) and (spath != "/"):
                site_path = spath[1:]
            pfiles.append(
                [file_page, {"url": page_meta.get_property("Url", None), "site_path": site_path}],
            )

        return pfiles

    def initialize(self):
        pass

    def _ingest_site_docs(self, site_client) -> List["SharepointIngestDoc"]:
        root_folder = site_client.web.get_folder_by_server_relative_path(self.config.path)
        if root_folder.select("Exists").get().execute_query().exists:
            logger.info(
                f"Folder {self.config.path} does not exist. Skipping site {site_client.base_url}",
            )
            return []
        files = self._list_files(root_folder, self.config.recursive)
        output = [SharepointIngestDoc(self.standard_config, self.config, f, None) for f in files]

        if self.config.process_pages:
            page_files = self._list_pages(site_client)
            page_output = [
                SharepointIngestDoc(self.standard_config, self.config, f[0], f[1])
                for f in page_files
            ]
            output = output + page_output
        return output

    def _filter_site_url(self, site):
        if site.url is None:
            return False
        return (
            (site.url[0:len(self.base_site_url)] == self.base_site_url)
            and ("/sites/" in site.url)
            and all(c == "0" for c in site.get_property("GroupId", "").replace("-", ""))
        )  # checks if its not a group, NOTE: do we want to process sharepoint groups?

    @requires_dependencies(["office365"])
    def get_ingest_docs(self):
        if self.config.process_all:
            from office365.runtime.auth.client_credential import ClientCredential
            from office365.sharepoint.client_context import ClientContext
            from office365.sharepoint.tenant.administration.tenant import Tenant

            tenant = Tenant(self.client)
            tenant_sites = tenant.get_site_properties_from_sharepoint_by_filters().execute_query()
            tenant_sites = [s.url for s in tenant_sites if self._filter_site_url(s)]
            tenant_sites.append(self.base_site_url)
            ingest_docs = []
            for site_url in set(tenant_sites):
                logger.info(f"Processing docs for site: {site_url}")
                site_client = ClientContext(site_url).with_credentials(
                    ClientCredential(self.config.client_id, self.config.client_credential),
                )
                ingest_docs = ingest_docs + self._ingest_site_docs(site_client)
            return ingest_docs
        else:
            return self._ingest_site_docs(self.client)
