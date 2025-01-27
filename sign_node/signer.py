# -*- mode:python; coding:utf-8; -*-
# author: Eugene Zamriy <ezamriy@cloudlinux.com>
# created: 2018-03-31


import enum
import os
import json
import logging
import pprint
import shutil
import glob
import time
import traceback
import tempfile
import typing
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib3 import Retry

import websocket
import requests
import requests.adapters
import plumbum
import pexpect
import rpm
import pgpy

from sign_node.errors import SignError
from sign_node.utils.file_utils import download_file, hash_file, safe_mkdir
from sign_node.uploaders.pulp import PulpRpmUploader
from sign_node.package_sign import (
    sign_dsc_package, sign_deb_package, sign_rpm_package
)


__all__ = ["Signer"]


class SignStatusEnum(enum.IntEnum):
    SUCCESS = 1
    READ_ERROR = 2
    NO_SIGNATURE = 3
    WRONG_SIGNATURE = 4


class Signer(object):
    def __init__(self, config, password_db, gpg):
        self.__config = config
        self.__password_db = password_db
        self.__gpg = gpg
        self.__pulp_uploader = PulpRpmUploader(
            self.__config.pulp_host,
            self.__config.pulp_user,
            self.__config.pulp_password,
            self.__config.pulp_chunk_size,
        )
        self.__download_credentials = {
            "login": config.node_id,
            "password": config.jwt_token,
        }
        if config.development_mode:
            self.__download_credentials["no_ssl_verify"] = True
        self.__session = self.__generate_request_session()

    def __generate_request_session(self):
        retry_strategy = Retry(
            total=10,
            backoff_factor=1,
            raise_on_status=True,
        )
        adapter = requests.adapters.HTTPAdapter(
            max_retries=retry_strategy)
        session = requests.Session()
        session.headers.update({
            'Authorization': f'Bearer {self.__config.jwt_token}',
        })
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        return session

    def sync_sign_loop(self):
        while True:
            try:
                queue = websocket.WebSocketApp(
                    urllib.parse.urljoin(
                        self.__config.ws_master_url,
                        'sign_task_queue/'
                    ),
                    on_message=self.on_sync_request,
                    header={
                        'Authorization': f'Bearer {self.__config.jwt_token}'
                    }
                )
                queue.run_forever(ping_interval=60)
            except Exception:
                logging.exception('Sync queue recieved exception:')

    def on_sync_request(self, queue, message):
        answer = {}
        try:
            payload = json.loads(message)
            password = self.__password_db.get_password(
                payload['key_id']
            )
            with tempfile.NamedTemporaryFile(mode='w') as fd:
                fd.write(payload['content'])
                fd.flush()
                sign_cmd = plumbum.local['gpg'][
                    '--yes', '--detach-sign', '--armor',
                    '--default-key', payload['key_id'], fd.name
                ]
                out, status = pexpect.run(
                    command=' '.join(sign_cmd.formulate()),
                    events={"Enter passphrase:.*": "{0}\r".format(password)},
                    env={"LC_ALL": "en_US.UTF-8"},
                    timeout=1200,
                    withexitstatus=1,
                )
                if status != 0:
                    message = f'gpg failed to sign file, error: {out}'
                    logging.error(message)
                    raise Exception(message)
                answer['asc_content'] = open(f'{fd.name}.asc', 'r').read()
                os.unlink(f'{fd.name}.asc')
        except Exception:
            answer['error'] = traceback.format_exc()
        queue.send(json.dumps(answer))

    def sign_loop(self):
        while True:
            task = None
            try:
                task = self._request_task()
            except Exception:
                logging.exception('Can\'t recieve new task from web server')
            if not task:
                logging.debug("There is no task to sign")
                time.sleep(30)
                continue
            logging.info(
                "Signing the following task:\n%s", pprint.pformat(task)
            )
            try:
                self._sign_build(task)
                logging.info("the %s task is signed", task["id"])
            except Exception as e:
                msg = (
                    f'Signing failed: {e}.\n'
                    f'Traceback: {traceback.format_exc()}'
                )
                logging.error(msg)
                response_payload = {'build_id': task['build_id'],
                                    'success': False, 'error_message': msg}
                self._report_signed_build(task['id'], response_payload)
                continue

    def _check_signature(self, files, key_id):
        errors = []
        key_id_lower = key_id.lower()
        ts = rpm.TransactionSet()
        ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)
        subkeys = [i.lower() for i in self.__password_db.get_subkeys(key_id)]

        def check(pkg_path: str) -> typing.Tuple[SignStatusEnum, str]:
            if not os.path.exists(pkg_path):
                return SignStatusEnum.READ_ERROR, ''

            with open(pkg_path, 'rb') as fd:
                header = ts.hdrFromFdno(fd)
                signature = header[rpm.RPMTAG_SIGGPG]
                if not signature:
                    signature = header[rpm.RPMTAG_SIGPGP]
                if not signature:
                    return SignStatusEnum.NO_SIGNATURE, ''

            pgp_msg = pgpy.PGPMessage.from_blob(signature)
            sig = ''
            for signature in pgp_msg.signatures:
                sig = signature.signer.lower()
                if sig == key_id_lower:
                    return SignStatusEnum.SUCCESS, ''
                elif subkeys and sig in subkeys:
                    return SignStatusEnum.SUCCESS, ''

            return SignStatusEnum.WRONG_SIGNATURE, sig

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {}
            for file_ in files:
                futures[executor.submit(check, file_)] = file_

            for future in as_completed(futures):
                pkg_path = futures[future]
                result, signature = future.result()
                if result == SignStatusEnum.READ_ERROR:
                    errors.append(f'Cannot read file {pkg_path}')
                elif result == SignStatusEnum.NO_SIGNATURE:
                    errors.append(f'Package {pkg_path} is not signed')
                elif result == SignStatusEnum.WRONG_SIGNATURE:
                    errors.append(f'Package {pkg_path} is signed '
                                  f'with the wrong key: {signature}')

        return errors

    def _sign_build(self, task):
        """
        Signs packages from the specified task and uploads them to the server.

        Parameters
        ----------
        task : dict
            Sign task.
        """
        pgp_keyid = task["keyid"]
        pgp_key_password = self.__password_db.get_password(pgp_keyid)
        fingerprint = self.__password_db.get_fingerprint(pgp_keyid)
        task_dir = os.path.join(self.__config.working_dir, str(task["id"]))
        rpms_dir = os.path.join(task_dir, "rpms")
        debs_dir = os.path.join(task_dir, "debs")
        downloaded = []
        has_rpms = False
        response_payload = {'build_id': task['build_id'], 'success': True}
        packages = {}
        try:
            for package in task["packages"]:
                package_type = package.get("type", "rpm")
                if package_type in ("deb", "dsc"):
                    download_dir = debs_dir
                else:
                    download_dir = rpms_dir
                    has_rpms = True
                package_path = self._download_package(download_dir, package)
                downloaded.append(
                    (package["id"], package["name"], package_path)
                )
                if package_type == "dsc":
                    sign_dsc_package(
                        self.__gpg, package_path, pgp_keyid, pgp_key_password
                    )
                elif package_type == "deb":
                    sign_deb_package(
                        self.__gpg, package_path, pgp_keyid, pgp_key_password
                    )
                # Preparing the payload for returning to web server
                signed_package = package.copy()
                signed_package['fingerprint'] = fingerprint
                signed_package.pop('download_url')
                packages[package['id']] = signed_package
            if has_rpms:
                packages_to_sign = []
                for package in glob.glob(os.path.join(rpms_dir, '*/*.rpm')):
                    packages_to_sign.append(package)
                    if len(packages_to_sign) % 50 == 0:
                        sign_rpm_package(
                            ' '.join(packages_to_sign),
                            pgp_keyid,
                            pgp_key_password,
                        )
                        packages_to_sign = []
                if packages_to_sign:
                    sign_rpm_package(
                        ' '.join(packages_to_sign),
                        pgp_keyid,
                        pgp_key_password,
                    )
            # upload signed packages and report the task completion
            files_to_upload = {}
            packages_hrefs = {}
            files_to_check = list()
            for package_id, file_name, package_path in downloaded:
                sha256 = hash_file(package_path, hash_type='sha256')
                if sha256 not in files_to_upload:
                    files_to_upload[sha256] = (
                        package_id, file_name, package_path)
                    files_to_check.append(package_path)
                packages[package_id]['sha256'] = sha256

            sign_errors = self._check_signature(files_to_check, pgp_keyid)
            if sign_errors:
                error_message = 'Errors during checking packages ' \
                                'signatures: \n{}'.format('\n'.join(sign_errors))
                logging.error(error_message)
                raise SignError(error_message)

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = {
                    executor.submit(
                        self._upload_artifact, package_path): package_id
                    for package_id, file_name, package_path
                    in files_to_upload.values()
                }
                for future in as_completed(futures):
                    result = future.result()
                    package_id = futures[future]
                    package_name = packages[package_id]['name']
                    packages[package_id]['href'] = result.href
                    packages_hrefs[package_name] = result.href
            # Fill href for packages of the same architecture
            for id_, package in packages.items():
                if not package.get('href'):
                    packages[id_]['href'] = packages_hrefs[package['name']]
            response_payload['packages'] = list(packages.values())
        except Exception:
            error_message = traceback.format_exc()
            response_payload['success'] = False
            response_payload['error_message'] = error_message
        finally:
            logging.info('Response payload:')
            logging.info(response_payload)
            self._report_signed_build(task["id"], response_payload)
            if os.path.exists(task_dir):
                shutil.rmtree(task_dir)

    def _report_signed_build(self, task_id, response_payload):
        """
        Reports a build sign completion to the master.

        Parameters
        ----------
        task_id : str
            Sign task identifier.
        """
        response = self.__call_master(f'{task_id}/complete',
                                      **response_payload)
        if not response["success"]:
            raise Exception(
                "Server side error: {0}".format(response.get("error", "unknown"))
            )

    def _upload_artifact(self, file_path):
        artifacts_dir = os.path.dirname(file_path)
        logging.info('Artifacts dir: %s', artifacts_dir)
        logging.info(
            "Uploading %s signed package", os.path.basename(file_path)
        )
        return self.__pulp_uploader.upload_single_file(file_path)

    def _download_package(self, download_dir, package, try_count=3):
        """
        Downloads the specified package from the Build System server and checks
        the download file checksum.

        Parameters
        ----------
        download_dir : str
            Download directory base path.
        package : dict
            Package information.
        try_count : int, optional
            The number of download tries before aborting.

        Returns
        -------
        str
            Downloaded file path.

        Raises
        ------
        castor.errors.ConnectionError
            If the package download is failed.
        """
        package_dir = os.path.join(download_dir, str(package["id"]))
        safe_mkdir(package_dir)
        package_path = os.path.join(package_dir, package["name"])
        download_url = package["download_url"]
        last_exc = None
        for i in range(1, try_count + 1):
            logging.debug("Downloading %s %d/%d", download_url, i, try_count)
            try:
                download_file(download_url, package_path)
                # FIXME: check checksum later
                # checksum = hash_file(package_path, get_hasher("sha256"))
                # if checksum != package["checksum"]:
                #     raise ValueError(f"Checksum does not match for {download_url}.")
                return package_path
            except Exception as e:
                last_exc = e
                logging.error(
                    "Cannot download %s: %s.\nTraceback:\n%s",
                    download_url, str(e), traceback.format_exc()
                )
        raise last_exc

    def _request_task(self):
        """
        Requests a new signing task from the master.

        Returns
        -------
        dict or None
            Task to process or None if master didn't return a task.
        """
        pgp_keyids = self.__config.pgp_keys
        response = self.__call_master(
            "get_sign_task", key_ids=pgp_keyids
        )
        return response

    def __call_master(self, endpoint, **parameters):
        full_url = urllib.parse.urljoin(
            self.__config.master_url, f"sign-tasks/{endpoint}/"
        )
        response = self.__session.post(full_url, json=parameters, timeout=10)
        response.raise_for_status()
        return response.json()
