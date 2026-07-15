"""Simple file downloader with tqdm progress bar."""

import os
import time
import logging
from curl_cffi import requests
from huggingface_hub import hf_hub_url
from tqdm import tqdm

from raven.status._status import Status

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1 * 1024 * 1024  # 1MB
MAX_RETRIES = 3
RETRY_DELAYS = [1.0, 2.0, 4.0]


class Downloader:
    """Download files from HuggingFace Hub with progress display."""

    @staticmethod
    def download(
        repo_id: str,
        file_name: str,
        local_dir: str,
        progress: bool = True,
    ) -> str:
        """
        Download a single file from HF Hub.
        
        Args:
            repo_id: HuggingFace repo ID (e.g., "gpustack/bge-m3-GGUF")
            file_name: File name in the repo (e.g., "bge-m3-F16.gguf")
            local_dir: Local directory to save the file
            progress: Whether to show tqdm progress bar
            
        Returns:
            Path to downloaded file
            
        Raises:
            RuntimeError: If download fails after retries
        """
        os.makedirs(local_dir, exist_ok=True)
        url = hf_hub_url(repo_id=repo_id, filename=file_name)
        local_path = os.path.join(local_dir, file_name)
        temp_path = local_path + ".download"

        # Check if already complete
        if os.path.exists(local_path):
            file_size = os.path.getsize(local_path)
            logger.info(f"File already exists: {local_path} ({file_size} bytes)")
            return local_path

        attempt = 0
        last_error = None

        while attempt <= MAX_RETRIES:
            try:
                # Check for existing partial file (resume)
                resume_byte = 0
                if os.path.exists(temp_path):
                    resume_byte = os.path.getsize(temp_path)
                    logger.info(f"Resuming download from byte {resume_byte}")

                headers = {}
                if resume_byte > 0:
                    headers["Range"] = f"bytes={resume_byte}-"

                with requests.Session(impersonate="chrome120") as session:
                    response = session.get(url, headers=headers, stream=True, timeout=(30, 60))
                    response.raise_for_status()

                    total_size = 0
                    cl = response.headers.get("Content-Length")
                    if cl:
                        total_size = int(cl) + resume_byte

                    mode = "ab" if resume_byte > 0 else "wb"
                    
                    # Setup progress bar
                    pbar = None
                    if progress and total_size > 0:
                        pbar = tqdm(
                            total=total_size,
                            initial=resume_byte,
                            unit="B",
                            unit_scale=True,
                            unit_divisor=1024,
                            desc=file_name,
                            leave=False,
                        )

                    with open(temp_path, mode) as f:
                        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                            if not chunk:
                                continue
                            f.write(chunk)
                            f.flush()
                            if pbar:
                                pbar.update(len(chunk))

                    if pbar:
                        pbar.close()

                    # Verify size
                    actual_size = os.path.getsize(temp_path)
                    if total_size > 0 and actual_size != total_size:
                        raise IOError(f"Size mismatch: expected {total_size}, got {actual_size}")

                    os.replace(temp_path, local_path)
                    logger.info(f"Download complete: {local_path}")
                    return local_path

            except requests.exceptions.Timeout:
                last_error = "Timeout"
            except requests.exceptions.RequestException as e:
                last_error = str(e)

            attempt += 1
            if attempt <= MAX_RETRIES:
                delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                logger.warning(f"Retry {attempt}/{MAX_RETRIES} for {file_name} in {delay}s: {last_error}")
                time.sleep(delay)
            else:
                # Clean up partial file on final failure
                if os.path.exists(temp_path):
                    os.remove(temp_path)

        raise RuntimeError(f"Failed to download {file_name} from {repo_id} after {MAX_RETRIES} retries: {last_error}")

    @staticmethod
    def download_multiple(
        repo_id: str,
        file_names: list[str],
        local_dir: str,
        progress: bool = True,
    ) -> list[str]:
        """Download multiple files from the same repo."""
        results = []
        for file_name in file_names:
            result = Downloader.download(repo_id, file_name, local_dir, progress)
            results.append(result)
        return results