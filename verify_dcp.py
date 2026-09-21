import os
import glob
import base64
import hashlib
import xml.etree.ElementTree as ET
import logging
from datetime import datetime

def setup_logger(dcp_dir):
    """Sets up a logger that writes to both the console and a log file."""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file_path = os.path.join(dcp_dir, f"dcp_verification_{timestamp}.log")
    
    logger = logging.getLogger("DCP_Verifier")
    logger.setLevel(logging.INFO)
    
    if not logger.handlers:
        file_handler = logging.FileHandler(log_file_path)
        console_handler = logging.StreamHandler()
        
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        
    return logger, log_file_path

def remove_namespace(tag):
    """Strips the SMPTE or Interop XML namespace from a tag."""
    return tag.split('}')[-1] if '}' in tag else tag

def verify_dcp(dcp_dir):
    logger, log_file_path = setup_logger(dcp_dir)
    logger.info(f"Starting DCP verification for: {dcp_dir}")
    
    # 1. Locate XML files
    assetmap_files = glob.glob(os.path.join(dcp_dir, '*ASSETMAP*'))
    pkl_files = glob.glob(os.path.join(dcp_dir, '*PKL*.xml'))
    
    if not assetmap_files:
        logger.critical("Could not find an ASSETMAP file in the directory.")
        return
    if not pkl_files:
        logger.critical("Could not find a PKL file in the directory.")
        return
    
    # 2. Parse ASSETMAP to build a UUID -> Filename mapping
    uuid_to_filename = {}
    try:
        tree = ET.parse(assetmap_files[0])
        for asset in tree.getroot().findall('.//*'):
            if remove_namespace(asset.tag) == 'Asset':
                uuid, path = None, None
                for child in asset.iter():
                    tag = remove_namespace(child.tag)
                    if tag == 'Id':
                        uuid = child.text.strip().replace('urn:uuid:', '')
                    elif tag == 'Path':
                        path = child.text.strip()
                if uuid and path:
                    # normpath ensures subdirectories use the correct Windows slashes
                    uuid_to_filename[uuid] = os.path.normpath(path)
    except Exception as e:
        logger.critical(f"Failed to parse ASSETMAP: {e}")
        return

    # 3. Parse PKL to get expected Hash and Size for each UUID
    uuid_to_hash_size = {}
    try:
        tree = ET.parse(pkl_files[0])
        for asset in tree.getroot().findall('.//*'):
            if remove_namespace(asset.tag) == 'Asset':
                uuid, hash_b64, size = None, None, None
                for child in asset.iter():
                    tag = remove_namespace(child.tag)
                    if tag == 'Id':
                        uuid = child.text.strip().replace('urn:uuid:', '')
                    elif tag == 'Hash':
                        hash_b64 = child.text.strip()
                    elif tag == 'Size':
                        size = int(child.text.strip())
                if uuid and hash_b64 and size is not None:
                    uuid_to_hash_size[uuid] = (hash_b64, size)
    except Exception as e:
        logger.critical(f"Failed to parse PKL: {e}")
        return

    # 4. Verify the files
    all_passed = True
    for uuid, (expected_hash, expected_size) in uuid_to_hash_size.items():
        filename = uuid_to_filename.get(uuid)
        
        if not filename or "PKL" in filename or "ASSETMAP" in filename:
            continue
            
        filepath = os.path.join(dcp_dir, filename)
        
        if not os.path.exists(filepath):
            logger.error(f"MISSING FILE: {filename}")
            all_passed = False
            continue
            
        # Size check
        actual_size = os.path.getsize(filepath)
        if actual_size != expected_size:
            logger.error(f"SIZE MISMATCH: {filename} (Expected: {expected_size}, Got: {actual_size})")
            all_passed = False
            continue
            
        # Hash check
        logger.info(f"Hashing {filename}...")
        sha1 = hashlib.sha1()
        
        try:
            # Read in 8MB chunks to keep memory usage low on massive MXF files
            with open(filepath, 'rb') as f:
                while chunk := f.read(8192 * 1024):
                    sha1.update(chunk)
            
            actual_hash = base64.b64encode(sha1.digest()).decode('utf-8')
            
            if actual_hash == expected_hash:
                logger.info(f"PASS - {filename} (Hash match)")
            else:
                logger.error(f"HASH MISMATCH (CORRUPTED): {filename} (Expected: {expected_hash}, Got: {actual_hash})")
                all_passed = False
        except Exception as e:
            logger.error(f"Failed to read or hash {filename}: {e}")
            all_passed = False

    if all_passed:
        logger.info("RESULT: SUCCESS. All files are present and hashes match perfectly.")
    else:
        logger.critical("RESULT: FAILED. The DCP is corrupt or missing files.")
        
    print(f"\nVerification log saved to: {log_file_path}")

if __name__ == "__main__":
    dcp_folder_path = input("Enter the path to the DCP folder: ")
    if os.path.exists(dcp_folder_path):
        verify_dcp(dcp_folder_path)
    else:
        print("Error: That directory does not exist.")