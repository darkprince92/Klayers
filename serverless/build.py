import os
import shutil
import logging
import hashlib
from datetime import datetime

import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def put_requirements_hash(package, version, requirements_txt, requirements_hash):

    dynamodb = boto3.client('dynamodb')
    item = {'package': {'S': package},
            'version': {'S': str(version)},
            'requirements': {'S': requirements_txt},
            'requirements_hash': {'S': requirements_hash},
            'created_date': {'S': datetime.now().isoformat()}}
    try:
        response = dynamodb.put_item(TableName=os.environ['REQS_DB'],
                                     Item=item)
        logger.info(f"Successfully written {package}:{version} status to DB with hash: {requirements_hash}")
    except ClientError as e:
        logger.error(f"{e.response['Error']['Code']}: {e.response['Error']['Message']} for item {item}")
        exit(1)

    return


def check_requirement_hash(package, requirements_hash):
    """
    Args:
      package: Package name
      requirements_hash: SHA256 hash of the requirements.txt file
    returns:
      exists: Boolean value of if the requirements_hash exists in the DB (package was built already)
    """

    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(os.environ['REQS_DB'])

    response = table.query(
        KeyConditionExpression=Key("package").eq(package) & Key("requirements_hash").eq(requirements_hash)
    )

    if len(response['Items']) > 0:
        hash_found = True
    else:
        hash_found = False

    return hash_found


def freeze_requirements(package, path, version):
    """
    Walks through path, looking for *.dist-info folders. Parses out the package name and versions
    returns: package name and version in requirements.txt format as a string
    """

    requirements = []
    for subdir, dirs, files in os.walk(path):

        # .dist-info
        for dir in dirs:
            if (str(dir)[-10:]) == '.dist-info':
                package_info = str(dir)[:-10].split('-')
                package_name = package_info[0]
                package_version = package_info[1]
                requirements.append(f"{package_name}=={package_version}")

        # PKG-INFO (.egg-info)
        for file in files:
            if str(file) == 'PKG-INFO' and subdir[-8:] == "egg-info":
                package_version, package_name = False, False
                with open(f"{subdir}/{file}", 'r') as pkg_info:
                    lines = pkg_info.readlines()
                    for line in lines:
                        if line[:8] == "Version:":
                            package_version = str(line[8:]).strip()
                        if line[:5] == "Name:":
                            package_name = str(line[5:]).strip()
                    if package_version and package_name:
                        requirements.append(f"{package_name}=={package_version}")

    requirements_txt = '\n'.join(sorted(requirements))
    requirements_hash = hashlib.sha256(requirements_txt.encode('utf-8')).hexdigest()

    return requirements_txt.strip(), requirements_hash


def upload_to_s3(zip_file, package, uploaded_file_name):
    """
    Args:
      zip_file: Location of zip file to be uploaded to S3 bucket
      package: Name of python package being uploaded
    return:
      uploaded_file_name: Name of file in S3 bucket
    """

    bucket_name = os.environ['BUCKET_NAME']

    s3 = boto3.resource('s3')
    s3.meta.client.upload_file(zip_file, bucket_name, uploaded_file_name)

    client = boto3.client('s3')
    response = client.list_objects_v2(
        Bucket=bucket_name,
        Prefix=package
    )

    logger.info(f"Uploaded {package}.zip with "
                f"size {response['Contents'][0]['Size']} "
                f"at {response['Contents'][0]['LastModified']} "
                f"to {bucket_name}")

    return uploaded_file_name


def zip_dir(dir_path, package):
    zip_file = f'/tmp/{package}'
    result = shutil.make_archive(base_name=zip_file,
                                 format="zip",
                                 base_dir=dir_path.split('/')[-1],
                                 root_dir="/tmp")
    logger.info(result)
    return f"{zip_file}.zip"


def delete_dir(dir):
    try:
        shutil.rmtree(dir)
        logger.info("Deleted previous version of package directory")
    except FileNotFoundError:
        logger.info("No previous installation detected")
    return True


def dir_size(path='.'):
    total = 0
    for entry in os.scandir(path):
        if entry.is_file():
            total += entry.stat().st_size
        elif entry.is_dir():
            total += dir_size(entry.path)
    return total


def install(package, package_dir):
    """"
    Args:
      package: Name of package to be queried
    return:
      path to zip file of final package
    """
    delete_dir(package_dir)
    import subprocess
    output = subprocess.run(["pip", "install", package, "-t", package_dir, '--quiet', '--upgrade', '--no-cache-dir'],
                            capture_output=True)
    logger.info(output)
    output = subprocess.run(["pip", "freeze", ">", "/tmp/requirements.txt"],
                            capture_output=True)
    logger.info(output)

    return package_dir


def main(event,context):

    package = event['package']
    version = event['version']
    license_info = event['license_info']

    package_dir = f"/tmp/python"
    uploaded_file_name = f'{package}.zip'

    package_dir = install(package, package_dir=package_dir)
    package_size = dir_size(package_dir)
    logger.info(f"Installed {package} into {package_dir} with size: {package_size}")

    requirements_txt, requirements_hash = freeze_requirements(package=package,
                                                              path=package_dir,
                                                              version=version)

    with open(f"{package_dir}/requirements.txt", 'w') as requirements_file:
        requirements_file.write(requirements_txt)

    zip_file = zip_dir(dir_path=package_dir,
                       package=package)
    logger.info(f"Zipped package info {zip_file}")

    if not check_requirement_hash(package=package,
                                  requirements_hash=requirements_hash):
        logger.info(f"Requirements hash {requirements_hash} "
                    f" for {package}=={version} not previously built, proceeding to upload to S3")

        upload_to_s3(zip_file=zip_file,
                     package=package,
                     uploaded_file_name=uploaded_file_name)
        put_requirements_hash(package=package,
                              requirements_txt=requirements_txt,
                              requirements_hash=requirements_hash,
                              version=version)

        logger.info(f"Built package: {package}=={version} into s3://{os.environ['BUCKET_NAME']}"
                    f"file size {os.path.getsize(zip_file)} "
                    f"with requirements hash: {requirements_hash}")
    else:
        logger.info("Requirements hash previously built, proceeding to check for deployment")

    return {"zip_file": uploaded_file_name,
            "package": package,
            "version": version,
            "requirements_hash": requirements_hash,
            "license_info": license_info}
