use bitcoin::Target;
use bitcoin_hashes::sha256d::Hash as Sha256dHash;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyString};

use hex;
use sha2::Digest;

type MerkleBranch = Vec<String>;

#[derive(FromPyObject, Clone)]
pub struct Job {
    #[pyo3(item)]
    prevhash: String,
    #[pyo3(item)]
    coinb1: String,
    #[pyo3(item)]
    coinb2: String,
    #[pyo3(item)]
    merkle_branches: MerkleBranch,
    #[pyo3(item)]
    version: String,
    #[pyo3(item)]
    nbits: String,
}

fn decode_hex(s: String) -> Result<Vec<u8>, PyErr> {
    hex::decode(s.trim_start_matches("0x"))
        .map_err(|e| PyValueError::new_err(e.to_string()))
        .into()
}

fn get_difficulty(hash: [u8; 32]) -> f64 {
    let target = Target::from_be_bytes(hash);
    target.difficulty_float()
}

#[pymodule(submodule, name = "difficulty")]
pub mod difficulty {
    use primitive_types::U256;

    use super::*;

    fn handle_version(version: Option<Vec<u8>>, job_version: Vec<u8>) -> Result<Vec<u8>, PyErr> {
        // Handle version - if miner provided version, XOR it with base version
        let version_bytes: Vec<u8> = match version {
            Some(version) => {
                let job_version = job_version;
                let version_bytes = version;
                let mut version_int = job_version
                    .iter()
                    .zip(version_bytes.iter())
                    .map(|(a, b)| a ^ b)
                    .collect::<Vec<u8>>();
                version_int.resize(4, 0); // pad to 4 bytes
                version_int
            }
            None => job_version,
        }
        .into_iter()
        .collect();

        Ok(version_bytes)
    }

    #[pyfunction(name = "handle_version", signature = (version, job_version))]
    pub fn py_handle_version(
        py: Python,
        version: Py<PyString>,
        job_version: Py<PyString>,
    ) -> PyResult<String> {
        let version = version.extract::<String>(py)?;
        let job_version = job_version.extract::<String>(py)?;
        let version_bytes = handle_version(Some(decode_hex(version)?), decode_hex(job_version)?)?;
        Ok(hex::encode(version_bytes))
    }

    #[pyfunction(name = "get_prevhash_hex", signature = (prevhash))]
    pub fn py_get_prevhash_hex(py: Python, prevhash: Py<PyString>) -> PyResult<String> {
        let prevhash = prevhash.extract::<String>(py)?;
        let prevhash_bytes = get_prevhash_hex(decode_hex(prevhash)?)?;
        Ok(hex::encode(prevhash_bytes))
    }

    fn get_prevhash_hex(prevhash: Vec<u8>) -> Result<Vec<u8>, PyErr> {
        let prevhash_bytes = prevhash
            .chunks(4)
            .map(|chunk| chunk.into_iter().rev().map(|c| *c).collect::<Vec<u8>>())
            .flatten()
            .collect::<Vec<u8>>();
        Ok(prevhash_bytes)
    }

    #[pyfunction(name = "get_block_header", signature = (version, prevhash, merkle_root, ntime, nbits, nonce))]
    pub fn py_get_block_header(
        py: Python,
        version: Py<PyString>,
        prevhash: Py<PyString>,
        merkle_root: Py<PyString>,
        ntime: Py<PyString>,
        nbits: Py<PyString>,
        nonce: Py<PyString>,
    ) -> PyResult<Py<PyBytes>> {
        let version = version.extract::<String>(py)?;
        let prevhash = prevhash.extract::<String>(py)?;
        let merkle_root = merkle_root.extract::<String>(py)?;
        let ntime = ntime.extract::<String>(py)?;
        let nbits = nbits.extract::<String>(py)?;
        let nonce = nonce.extract::<String>(py)?;

        get_block_header(
            decode_hex(version)?,
            decode_hex(prevhash)?,
            decode_hex(merkle_root)?,
            decode_hex(ntime)?,
            decode_hex(nbits)?,
            decode_hex(nonce)?,
        )
        .map(|h| PyBytes::new(py, &h).into())
    }

    #[pyfunction(name = "get_coinbase", signature = (coinb1, extranonce1, extranonce2, coinb2))]
    pub fn py_get_coinbase(
        py: Python,
        coinb1: Py<PyString>,
        extranonce1: Py<PyString>,
        extranonce2: Py<PyString>,
        coinb2: Py<PyString>,
    ) -> PyResult<String> {
        let coinb1 = coinb1.extract::<String>(py)?;
        let extranonce1 = extranonce1.extract::<String>(py)?;
        let extranonce2 = extranonce2.extract::<String>(py)?;
        let coinb2 = coinb2.extract::<String>(py)?;
        let coinbase = get_coinbase(coinb1, extranonce1, extranonce2, coinb2)?;
        Ok(hex::encode(coinbase))
    }

    fn get_coinbase(
        coinb1: String,
        extranonce1: String,
        extranonce2: String,
        coinb2: String,
    ) -> Result<Vec<u8>, PyErr> {
        // Build coinbase transaction
        let coinbase_str: Vec<String> = [coinb1, extranonce1, extranonce2, coinb2]
            .iter()
            .map(|s| s.trim_start_matches("0x").to_string())
            .collect();

        let coinbase_bytes = decode_hex(coinbase_str.join(""))?;

        Ok(coinbase_bytes)
    }

    #[pyfunction(name = "get_merkle_root", signature = (coinbase, merkle_branches))]
    pub fn py_get_merkle_root(
        py: Python,
        coinbase: Py<PyString>,
        merkle_branches: Py<PyList>,
    ) -> PyResult<String> {
        let coinbase = coinbase.extract::<String>(py)?;
        let merkle_branches = merkle_branches.extract::<Vec<String>>(py)?;
        let merkle_root = get_merkle_root(decode_hex(coinbase)?, merkle_branches)?;

        Ok(hex::encode(merkle_root))
    }

    fn get_merkle_root(coinbase: Vec<u8>, merkle_branches: Vec<String>) -> Result<Vec<u8>, PyErr> {
        let coinbase_hash = sha2::Sha256::digest(sha2::Sha256::digest(coinbase)).to_vec();
        let merkle_branches_hashes: Vec<Vec<u8>> = merkle_branches
            .iter()
            .map(|s| decode_hex(s.to_string()))
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| PyValueError::new_err(e.to_string()))?;

        let merkle_root: Vec<u8> = merkle_branches_hashes
            .iter()
            .fold(coinbase_hash, |root, h| {
                let mut root_bytes = root.clone();
                root_bytes.extend(h);

                let root_hash = sha2::Sha256::digest(sha2::Sha256::digest(root_bytes));
                root_hash.to_vec()
            });

        Ok(merkle_root)
    }

    fn swap_endianness_u32(h: Vec<u8>) -> Vec<u8> {
        let h_int = u32::from_be_bytes(h.try_into().unwrap());
        h_int.to_le_bytes().to_vec()
    }

    fn swap_endianness_u256(h: Vec<u8>) -> Vec<u8> {
        let h_int = U256::from_str_radix(&hex::encode(h), 16).unwrap();
        h_int.to_little_endian().to_vec()
    }

    fn get_block_header(
        version: Vec<u8>,
        prevhash: Vec<u8>,
        merkle_root: Vec<u8>,
        ntime: Vec<u8>,
        nbits: Vec<u8>,
        nonce: Vec<u8>,
    ) -> Result<Vec<u8>, PyErr> {
        // Build the header
        let mut header: Vec<u8> = Vec::new();
        header.extend(swap_endianness_u32(version)); // Version - 4 bytes LE
        header.extend(prevhash.into_iter()); // Previous hash - 32 bytes (8x4 LE chunks)
        header.extend(merkle_root.into_iter()); // Merkle root - 32 bytes
        header.extend(swap_endianness_u32(ntime)); // Timestamp - 4 bytes LE
        header.extend(swap_endianness_u32(nbits)); // Bits - 4 bytes LE
        header.extend(swap_endianness_u32(nonce)); // Nonce - 4 bytes LE

        Ok(header)
    }

    pub fn get_nbits_bytes(nbits: Vec<u8>) -> Result<Vec<u8>, PyErr> {
        Ok(nbits)
    }

    pub fn calculate_share_difficulty(
        job_obj: &Job,
        extranonce1: &String,
        extranonce2: &String,
        ntime: &Vec<u8>,
        nonce: &Vec<u8>,
        version: &Option<Vec<u8>>,
    ) -> Result<(f64, String), PyErr> {
        let coinbase = get_coinbase(
            job_obj.coinb1.to_string(),
            extranonce1.to_string(),
            extranonce2.to_string(),
            job_obj.coinb2.to_string(),
        )?;
        let merkle_root = get_merkle_root(coinbase, job_obj.merkle_branches.clone())?;
        let version_bytes = handle_version(version.clone(), decode_hex(job_obj.version.clone())?)?;
        let prevhash_bytes = get_prevhash_hex(decode_hex(job_obj.prevhash.clone())?)?;
        let nbits_bytes = get_nbits_bytes(decode_hex(job_obj.nbits.clone())?)?;

        let header = get_block_header(
            version_bytes,
            prevhash_bytes,
            merkle_root,
            ntime.clone(),
            nbits_bytes,
            nonce.clone(),
        )?;
        let block_hash = sha2::Sha256::digest(sha2::Sha256::digest(header));
        let block_hash_bytes = swap_endianness_u256(block_hash.to_vec());
        let difficulty = get_difficulty(block_hash_bytes.clone().try_into().unwrap());

        Ok((difficulty, hex::encode(block_hash_bytes.clone())))
    }

    #[pyfunction(name = "get_difficulty", signature = (hash))]
    pub fn py_get_difficulty(py: Python, hash: Py<PyString>) -> PyResult<f64> {
        let hash = hash.extract::<String>(py)?;
        let hash = decode_hex(hash)?;
        let difficulty = get_difficulty(hash.try_into().unwrap());
        Ok(difficulty)
    }

    #[pyfunction(name = "find_nonce", signature = (job, extranonce1, extranonce2, ntime, version, start, end, threshold))]
    pub fn py_find_nonce(
        py: Python,
        job: Py<PyDict>,
        extranonce1: Py<PyString>,
        extranonce2: Py<PyString>,
        ntime: Py<PyString>,
        version: Option<Py<PyString>>,
        start: u32,
        end: u32,
        threshold: f64,
    ) -> PyResult<(Vec<u8>, String)> {
        let job_obj = job.extract::<Job>(py)?;
        let extranonce1 = extranonce1.extract::<String>(py)?;
        let extranonce2 = extranonce2.extract::<String>(py)?;
        let ntime = ntime.extract::<String>(py)?;
        let version = match version {
            Some(v) => Some(decode_hex(v.extract::<String>(py)?)?),
            None => None,
        };
        let (nonce, block_hash) = find_nonce(
            job_obj,
            extranonce1,
            extranonce2,
            decode_hex(ntime)?,
            version,
            start,
            end,
            threshold,
        )?;
        Ok((nonce, block_hash))
    }

    pub fn find_nonce(
        job_obj: Job,
        extranonce1: String,
        extranonce2: String,
        ntime: Vec<u8>,
        version: Option<Vec<u8>>,
        start: u32,
        end: u32,
        threshold: f64,
    ) -> Result<(Vec<u8>, String), PyErr> {
        let job_obj = job_obj.clone();
        let extranonce1 = extranonce1.clone();
        let extranonce2 = extranonce2.clone();
        let ntime = ntime.clone();
        let version = version.clone();

        for nonce in start..end {
            let nonce = nonce.to_le_bytes().to_vec();
            let (difficulty, block_hash) = calculate_share_difficulty(
                &job_obj,
                &extranonce1,
                &extranonce2,
                &ntime,
                &nonce,
                &version,
            )?;
            if difficulty >= threshold {
                return Ok((nonce, hex::encode(block_hash)));
            }
        }
        Ok((vec![], "".to_string()))
    }

    /// Calculate the actual difficulty of a submitted share.
    /// Uses the double SHA256 algorithm to compute the hash and derives
    /// the difficulty from the hash value.
    ///
    /// Args:
    ///     job: Job data containing
    ///         prevhash: hexstr, 8xbytes, LE
    ///         coinb1: hexstr,
    ///         coinb2: hexstr,
    ///         merkle branches: list of hexstr,
    ///         nbits: hexstr,
    ///         version: hexstr
    ///     extranonce1: Extranonce1 assigned by pool (hexstr)
    ///     extranonce2: Extranonce2 provided by miner (hexstr)
    ///     ntime: Timestamp (hexstr)
    ///     nonce: Nonce value (hexstr)
    ///     version: Optional version string (hexstr | intstr)
    ///
    /// Returns:
    ///     Tuple of (difficulty, block_hash_hex)
    ///
    #[pyfunction(name = "calculate_share_difficulty", signature = (job, extranonce1, extranonce2, ntime, nonce, version = None))]
    pub fn py_calculate_share_difficulty(
        py: Python,
        job: Py<PyDict>,               //dict[str, Any],
        extranonce1: Py<PyString>,     //str,
        extranonce2: Py<PyString>,     //str,
        ntime: Py<PyString>,           //str,
        nonce: Py<PyString>,           //str,
        version: Option<Py<PyString>>, // Optional[str] = None,
    ) -> PyResult<(f64, String)> {
        // Initialize logging
        let _ = pyo3_log::try_init();

        let job_obj = job.extract::<Job>(py)?;
        let extranonce1 = extranonce1.extract::<String>(py)?;
        let extranonce2 = extranonce2.extract::<String>(py)?;
        let ntime = ntime.extract::<String>(py)?;
        let nonce = nonce.extract::<String>(py)?;
        let version = match version {
            Some(v) => Some(decode_hex(v.extract::<String>(py)?)?),
            None => None,
        };

        let (difficulty, block_hash) = calculate_share_difficulty(
            &job_obj,
            &extranonce1,
            &extranonce2,
            &decode_hex(ntime)?,
            &decode_hex(nonce)?,
            &version,
        )?;

        return Ok((difficulty, block_hash));

        // logger.error(f"Error calculating share difficulty: {e}", exc_info=True)
        // return 0.0, ""
    }

    pub fn block_header_from_job(
        job_obj: Job,
        extranonce1: String,
        extranonce2: String,
        ntime: Vec<u8>,
        nonce: Vec<u8>,
        version: Option<Vec<u8>>,
    ) -> Result<Vec<u8>, PyErr> {
        let coinbase = get_coinbase(job_obj.coinb1, extranonce1, extranonce2, job_obj.coinb2)?;
        let merkle_root = get_merkle_root(coinbase, job_obj.merkle_branches)?;
        let version_bytes = handle_version(version, decode_hex(job_obj.version)?)?;
        let prevhash_bytes = get_prevhash_hex(decode_hex(job_obj.prevhash)?)?;
        let nbits_bytes = get_nbits_bytes(decode_hex(job_obj.nbits)?)?;

        let block_header = get_block_header(
            version_bytes,
            prevhash_bytes,
            merkle_root,
            ntime,
            nbits_bytes,
            nonce,
        )?;

        Ok(block_header)
    }

    #[pyfunction(name = "block_header_from_job", signature = (job, extranonce1, extranonce2, ntime, nonce, version = None))]
    pub fn py_block_header_from_job(
        py: Python,
        job: Py<PyDict>,               //dict[str, Any],
        extranonce1: Py<PyString>,     //str,
        extranonce2: Py<PyString>,     //str,
        ntime: Py<PyString>,           //str,
        nonce: Py<PyString>,           //str,
        version: Option<Py<PyString>>, // Optional[str] = None,
    ) -> PyResult<Vec<u8>> {
        // Initialize logging
        let _ = pyo3_log::try_init();

        let job_obj = job.extract::<Job>(py)?;
        let extranonce1 = extranonce1.extract::<String>(py)?;
        let extranonce2 = extranonce2.extract::<String>(py)?;
        let ntime = ntime.extract::<String>(py)?;
        let nonce = nonce.extract::<String>(py)?;
        let version = match version {
            Some(v) => Some(decode_hex(v.extract::<String>(py)?)?),
            None => None,
        };

        let block_header = block_header_from_job(
            job_obj,
            extranonce1,
            extranonce2,
            decode_hex(ntime)?,
            decode_hex(nonce)?,
            version,
        )?;

        return Ok(block_header);
        // logger.error(f"Error calculating share difficulty: {e}", exc_info=True)
        // return 0.0, ""
    }
}
