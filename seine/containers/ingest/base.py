# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

from abc import ABC, abstractmethod
from typing import List


class ContainerIngestionHandler(ABC):
    """Base interface for container runtime ingestion handlers in the imager appliance."""

    @abstractmethod
    def generate_start_script(self, source_date_epoch: int) -> str:
        """Return the shell script block to initialize runtime daemons inside the appliance."""
        pass

    @abstractmethod
    def generate_import_script(self, dev: str, source_date_epoch: int) -> str:
        """Return the shell script block to import a single container archive."""
        pass

    @abstractmethod
    def generate_stop_script(self, source_date_epoch: int) -> str:
        """Return the shell script block to stop daemons and run normalization."""
        pass

    @abstractmethod
    def generate_normalize_script(self, source_date_epoch: int) -> str:
        """Return the Python/shell normalization script executed after ingestion."""
        pass

    def generate_ingest_script(self, dev_list: List[str], source_date_epoch: int) -> str:
        """Return the complete execution script combining start, import, and stop."""
        imports = "\n".join(self.generate_import_script(dev, source_date_epoch) for dev in sorted(dev_list))
        return f"{self.generate_start_script(source_date_epoch)}\n{imports}\n{self.generate_stop_script(source_date_epoch)}"

    def generate_script(self, dev_list: List[str], source_date_epoch: int) -> str:
        """Return the complete execution script combining ingestion and normalization."""
        return self.generate_ingest_script(dev_list, source_date_epoch)
