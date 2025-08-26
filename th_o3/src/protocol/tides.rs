use pyo3::prelude::*;
use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

use sqlite;

const DB_FETCH_SIZE: i64 = 100;

pub type UserId = i64;
pub type JobId = Vec<u8>;

#[derive(Debug)]
pub enum Error {
    DatabaseNotConnected,
    DatabaseError(sqlite::Error),
    DatabaseLockError,
    Unknown,
}

impl From<sqlite::Error> for Error {
    fn from(e: sqlite::Error) -> Self {
        Error::DatabaseError(e)
    }
}

#[pymodule(submodule, name = "tides")]
pub mod tides {
    use std::collections::HashMap;

    use super::*;

    #[pyclass]
    #[derive(Clone)]
    struct ShareIndex {
        pub index: i64,
    }

    #[pyclass]
    struct Database {
        pub connection: String,
        pub conn: Option<sqlite::ConnectionThreadSafe>,
    }

    impl Database {
        pub fn new(connection: String) -> Self {
            Self {
                connection,
                conn: None,
            }
        }

        pub fn connect(&mut self) -> Result<(), String> {
            let conn = sqlite::Connection::open_thread_safe(&self.connection)
                .map_err(|e| e.to_string())?;
            self.conn = Some(conn);
            Ok(())
        }

        pub fn init_db(&self) -> Result<(), Error> {
            if let Some(conn) = &self.conn {
                let query = "
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    btc_address TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    UNIQUE(btc_address) ON CONFLICT ROLLBACK
                )
                ";
                let res = conn.execute(query)?;
                println!("Users table created: {:?}", res);

                let query = "
                CREATE TABLE IF NOT EXISTS lightning_addresses (
                    lightning_address TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    signature TEXT NOT NULL,
                    UNIQUE(lightning_address) ON CONFLICT ROLLBACK,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
                ";
                let res = conn.execute(query)?;
                println!("Lightning addresses table created: {:?}", res);

                let query = "
                CREATE TABLE IF NOT EXISTS shares (
                    share_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    difficulty REAL NOT NULL,
                    block_hash BLOB,
                    nonce BLOB,
                    timestamp BLOB,
                    prevhash BLOB,
                    version BLOB,
                    job_version BLOB,
                    job_ntime BLOB,
                    job_nbits BLOB,
                    job_merkle_root BLOB,
                    job_coinb1 BLOB,
                    job_coinb2 BLOB,
                    job_extranonce1 BLOB,
                    job_extranonce2 BLOB,
                    fee_rate REAL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
                ";
                let res = conn.execute(query)?;
                println!("Shares table created: {:?}", res);
                Ok(())
            } else {
                return Err(Error::DatabaseNotConnected);
            }
        }

        pub fn add_user_by_btc_address(&self, btc_address: String) -> Result<UserId, Error> {
            if let Some(conn) = &self.conn {
                let check_query = "SELECT user_id FROM users WHERE btc_address = ?";
                let mut stmt = conn.prepare(check_query)?;
                stmt.bind((1, btc_address.as_str()))?;
                if stmt.next().is_ok() {
                    Ok(stmt.read::<UserId, _>(0)?)
                } else {
                    let query = "
                        INSERT INTO users (btc_address) VALUES (?)
                        RETURNING user_id
                    ";
                    let mut stmt = conn.prepare(query)?;
                    stmt.bind((1, btc_address.as_str()))?;
                    stmt.next()?;

                    println!("User added by BTC address: {:?}", btc_address);
                    Ok(stmt.read::<UserId, _>(0)?)
                }
            } else {
                return Err(Error::DatabaseNotConnected);
            }
        }

        pub fn get_btc_address_for_user(&self, user_id: i64) -> Result<String, Error> {
            if let Some(conn) = &self.conn {
                let query = "SELECT btc_address FROM users WHERE user_id = ?";
                let mut stmt = conn.prepare(query)?;
                stmt.bind((1, user_id))?;
                stmt.next()?;
                let btc_address = stmt.read::<String, _>(0)?;
                Ok(btc_address)
            } else {
                return Err(Error::DatabaseNotConnected);
            }
        }

        pub fn add_share(&self, share: Share) -> Result<(), Error> {
            if let Some(conn) = &self.conn {
                let query = "
                INSERT INTO shares (
                    user_id,
                    difficulty,
                    block_hash,
                    nonce,
                    timestamp,
                    prevhash,
                    version,
                    job_version,
                    job_ntime,
                    job_nbits,
                    job_merkle_root,
                    job_coinb1,
                    job_coinb2,
                    job_extranonce1,
                    job_extranonce2,
                    fee_rate,
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ";
                let mut stmt = conn.prepare(query)?;
                stmt.bind((1, share.user_id))?;
                stmt.bind((2, share.difficulty))?;
                stmt.bind((3, share.block_hash.as_slice()))?;
                stmt.bind((4, share.nonce.as_slice()))?;
                stmt.bind((5, i64::try_from(share.timestamp).unwrap_or(i64::MAX)))?;
                stmt.bind((6, share.prevhash.as_slice()))?;
                stmt.bind((7, share.version.as_slice()))?;
                stmt.bind((8, share.job_version.as_slice()))?;
                stmt.bind((9, share.job_ntime.as_slice()))?;
                stmt.bind((10, share.job_nbits.as_slice()))?;
                stmt.bind((11, share.job_merkle_root.as_slice()))?;
                stmt.bind((12, share.job_coinb1.as_slice()))?;
                stmt.bind((13, share.job_coinb2.as_slice()))?;
                stmt.bind((14, share.job_extranonce1.as_slice()))?;
                stmt.bind((15, share.job_extranonce2.as_slice()))?;
                stmt.bind((16, share.fee_rate))?;

                stmt.next()?;
                println!("Added share to database: {:?}", share.difficulty);

                Ok(())
            } else {
                return Err(Error::DatabaseNotConnected);
            }
        }

        pub fn get_shares(
            &self,
            start_index: ShareIndex,
            max_count: i64,
        ) -> Result<Vec<Share>, Error> {
            if let Some(conn) = &self.conn {
                let query = "
                SELECT * FROM shares WHERE share_id <= ? ORDER BY share_id DESC LIMIT ?
                ";
                let mut stmt = conn.prepare(query)?;
                stmt.bind((1, start_index.index))?;
                stmt.bind((2, max_count))?;

                let mut shares = Vec::new();
                while let Ok(state) = stmt.next() {
                    if matches!(state, sqlite::State::Done) {
                        break;
                    }

                    let share: Share = {
                        Share {
                            user_id: stmt.read::<i64, _>("user_id")?,
                            difficulty: stmt.read::<f64, _>("difficulty")?,
                            block_hash: stmt.read::<Vec<u8>, _>("block_hash")?,
                            nonce: stmt.read::<Vec<u8>, _>("nonce")?,
                            timestamp: stmt.read::<i64, _>("timestamp")?.try_into().unwrap(),
                            prevhash: stmt.read::<Vec<u8>, _>("prevhash")?,
                            version: stmt.read::<Vec<u8>, _>("version")?,
                            index: ShareIndex {
                                index: stmt.read::<i64, _>("share_id")?,
                            },
                            job_version: stmt.read::<Vec<u8>, _>("job_version")?,
                            job_ntime: stmt.read::<Vec<u8>, _>("job_ntime")?,
                            job_nbits: stmt.read::<Vec<u8>, _>("job_nbits")?,
                            job_merkle_root: stmt.read::<Vec<u8>, _>("job_merkle_root")?,
                            job_coinb1: stmt.read::<Vec<u8>, _>("job_coinb1")?,
                            job_coinb2: stmt.read::<Vec<u8>, _>("job_coinb2")?,
                            job_extranonce1: stmt.read::<Vec<u8>, _>("job_extranonce1")?,
                            job_extranonce2: stmt.read::<Vec<u8>, _>("job_extranonce2")?,
                            diff_used: 0., // start at 0. none used yet.
                            fee_rate: stmt.read::<f64, _>("fee_rate")?,
                        }
                    };
                    shares.push(share);
                }
                Ok(shares)
            } else {
                Err(Error::DatabaseNotConnected)
            }
        }
    }

    #[pyclass]
    #[derive(Clone)]
    struct Share {
        pub user_id: i64,             // user for the share
        pub difficulty: f64,          // difficulty of the share
        pub block_hash: Vec<u8>,      // block hash of the share
        pub nonce: Vec<u8>,           // nonce of the share
        pub timestamp: u64,           // timestamp of the share
        pub prevhash: Vec<u8>,        // prevhash of the share
        pub version: Vec<u8>,         // version of the share
        pub index: ShareIndex,        // index of the share in the database
        pub job_version: Vec<u8>,     // job version of the share
        pub job_ntime: Vec<u8>,       // job ntime of the share
        pub job_nbits: Vec<u8>,       // job nbits of the share
        pub job_merkle_root: Vec<u8>, // job merkle root of the share
        pub job_coinb1: Vec<u8>,      // job coinb1 of the share
        pub job_coinb2: Vec<u8>,      // job coinb2 of the share
        pub job_extranonce1: Vec<u8>, // job extranonce1 of the share
        pub job_extranonce2: Vec<u8>, // job extranonce2 of the share
        pub diff_used: f64,           // difficulty used to calculate the share
        pub fee_rate: f64,            // fee rate of the share
    }

    #[pyclass]
    struct ShareDeque {
        shares: VecDeque<Share>,
        pub curr_difficulty: f64,       // current difficulty
        pub share_log_window_size: u32, // multiple of current difficulty
        pub curr_sum: f64,              // current sum of the share queue
    }

    impl ShareDeque {
        pub fn new(share_log_window_size: u32, curr_difficulty: f64) -> Self {
            Self {
                shares: VecDeque::new(),
                curr_difficulty,
                share_log_window_size,
                curr_sum: 0.0,
            }
        }

        pub fn share_log_window(&self) -> f64 {
            self.share_log_window_size as f64 * self.curr_difficulty
        }

        pub fn get_shares_in_window(&self) -> Vec<Share> {
            // All shares in the queue are in the window
            self.shares.iter().map(|share| share.clone()).collect()
        }

        pub fn add_share(&mut self, share: Share) -> Vec<Share> {
            self.shares.push_back(share);
            let share_log_window = self.share_log_window();
            let mut to_remove = self.curr_sum - share_log_window;

            let mut removed: Vec<Share> = Vec::new();
            while to_remove > 0.0 {
                let Some(share) = self.shares.get_mut(0) else {
                    panic!("Share queue is empty but curr_sum is greater than share_log_window");
                };
                if share.diff_used > to_remove {
                    share.diff_used -= to_remove;
                    self.curr_sum -= to_remove;
                    break;
                } else {
                    self.curr_sum -= share.diff_used;
                    share.diff_used = 0.0;
                    removed.push(share.clone());
                    to_remove -= share.diff_used;

                    self.shares.pop_front();
                }
            }

            return removed;
        }

        // Adjust the difficulty and return any shares that were removed
        pub fn adjust_difficulty(&mut self, difficulty: f64) -> Vec<Share> {
            self.curr_difficulty = difficulty;

            let new_share_log_window = self.share_log_window();
            let mut to_remove = self.curr_sum - new_share_log_window;
            let mut removed: Vec<Share> = Vec::new();

            if to_remove > 0.0 {
                while to_remove > 0.0 {
                    let Some(share) = self.shares.get_mut(0) else {
                        panic!(
                            "Share queue is empty but curr_sum is greater than share_log_window"
                        );
                    };
                    if share.diff_used > to_remove {
                        share.diff_used -= to_remove;
                        self.curr_sum -= to_remove;
                        break;
                    } else {
                        self.curr_sum -= share.diff_used;
                        share.diff_used = 0.0;
                        removed.push(share.clone());
                        to_remove -= share.diff_used;

                        self.shares.pop_front();
                    }
                }
            }
            return removed;
        }

        // Takes these in-order, highest index first
        // Only adds shares if the share window is not full
        pub fn push_front_shares(&mut self, shares: Vec<Share>) {
            let mut curr = 0;
            let mut to_add = self.share_log_window() - self.curr_sum;
            while to_add > 0.0 {
                let Some(share) = shares.get(curr) else {
                    break;
                };

                if share.difficulty < to_add {
                    self.curr_sum += share.difficulty;
                    self.shares.push_front(share.clone());
                    to_add -= share.difficulty;
                } else {
                    // The share needs to be split
                    let mut new_share = share.clone();
                    new_share.diff_used = to_add;
                    self.curr_sum += to_add;
                    self.shares.push_front(new_share);
                    // to_add is now 0.0

                    break;
                }
                curr += 1;
            }
        }
    }

    struct ShareBufferDeque {
        shares: VecDeque<Share>,
        pub curr_sum: f64, // current sum of the share queue
    }

    impl ShareBufferDeque {
        pub fn new() -> Self {
            Self {
                shares: VecDeque::new(),
                curr_sum: 0.0,
            }
        }

        pub fn add_share(&mut self, share: Share) {
            self.curr_sum += share.difficulty;
            self.shares.push_back(share);
        }

        pub fn remove_share(&mut self) {
            if let Some(share) = self.shares.pop_front() {
                self.curr_sum -= share.difficulty;
            }
        }

        pub fn pop_back(&mut self) -> Option<Share> {
            let Some(share) = self.shares.pop_back() else {
                return None;
            };
            self.curr_sum -= share.difficulty;
            Some(share)
        }

        pub fn truncate(&mut self, max_size: usize) -> Vec<Share> {
            let mut removed: Vec<Share> = Vec::new();
            while self.shares.len() > max_size {
                let Some(share) = self.shares.pop_front() else {
                    panic!("Share buffer is empty but curr_sum is greater than share_log_window");
                };
                removed.push(share);
            }
            return removed;
        }

        pub fn extend(&mut self, shares: Vec<Share>) {
            for share in shares {
                self.add_share(share);
            }
        }

        pub fn last_share(&self) -> Option<Share> {
            self.shares.back().cloned()
        }
    }

    #[pyclass]
    struct Tides {
        pub share_queue: ShareDeque,
        pub share_buffer: ShareBufferDeque,
        pub share_buffer_size: usize,
        pub curr_difficulty: f64,  // current difficulty
        pub share_log_window: u32, // multiple of current difficulty
        pub database: Arc<Mutex<Database>>,
    }

    impl Tides {
        pub fn new(
            share_log_window: u32,
            curr_difficulty: f64,
            share_buffer_size: usize,
            database: Arc<Mutex<Database>>,
        ) -> Self {
            Self {
                share_queue: ShareDeque::new(share_log_window, curr_difficulty),
                share_buffer: ShareBufferDeque::new(),
                share_buffer_size,
                curr_difficulty,
                share_log_window,
                database,
            }
        }

        pub fn fill_share_queue(&mut self) -> Result<(), Error> {
            // Then grab some shares from the buffer
            let diff_to_add = self.share_queue.share_log_window() - self.share_queue.curr_sum;
            if diff_to_add <= 0.0 {
                // Window is full
                return Ok(());
            }

            // Check if we need to add more shares from the DB
            if diff_to_add > self.share_buffer.curr_sum {
                let mut overage = diff_to_add - self.share_buffer.curr_sum;

                let mut last_index = ShareIndex { index: 0 };
                if let Some(last_share) = self.share_buffer.last_share() {
                    last_index = last_share.index;
                }

                let mut shares_from_db = Vec::new();
                let db = self.database.lock();
                if let Ok(db) = db {
                    while overage > 0.0 {
                        let shares_ = db.get_shares(last_index.clone(), DB_FETCH_SIZE)?;

                        for share in shares_.iter() {
                            shares_from_db.push(share.clone());
                            overage -= share.difficulty;
                        }
                        if let Some(last_share) = shares_.last() {
                            last_index = last_share.index.clone();
                        } else {
                            break; // No more shares in the DB
                        }
                    }
                } else {
                    println!("Failed to lock database");
                }

                // Add the shares from the DB to the buffer
                self.share_buffer.extend(shares_from_db.clone());
            }

            // Add shares from the buffer to the queue until the queue is full or the buffer is empty
            while self.share_queue.curr_sum < self.share_queue.share_log_window() {
                let Some(share) = self.share_buffer.pop_back() else {
                    break;
                };
                self.share_queue.push_front_shares(vec![share]);
            }

            Ok(())
        }

        pub fn restore_share_queue(&mut self) -> Result<(), Error> {
            self.fill_share_queue()?; // Fill from DB
            Ok(())
        }

        pub fn add_share(&mut self, share: Share) -> Result<(), Error> {
            self.add_share_to_db(share.clone())?; // Add to DB

            let removed = self.share_queue.add_share(share);
            self.share_buffer.extend(removed);
            let _ = self.share_buffer.truncate(self.share_buffer_size);

            Ok(())
        }

        pub fn adjust_difficulty(&mut self, difficulty: f64) -> Result<(), Error> {
            if difficulty == self.curr_difficulty {
                return Ok(());
            }

            let old_difficulty = self.curr_difficulty;
            self.curr_difficulty = difficulty;

            let removed = self.share_queue.adjust_difficulty(difficulty);
            self.share_buffer.extend(removed);
            let _ = self.share_buffer.truncate(self.share_buffer_size);

            if old_difficulty > difficulty {
                self.fill_share_queue()?; // Fill window

                Ok(())
            } else {
                Ok(()) // Already removed above
            }
        }

        pub fn get_btc_address_for_user(&self, user_id: i64) -> Result<String, Error> {
            let db = self.database.lock();
            if let Ok(db) = db {
                db.get_btc_address_for_user(user_id)
            } else {
                Err(Error::DatabaseLockError)
            }
        }

        fn add_share_to_db(&mut self, share: Share) -> Result<(), Error> {
            let db = self
                .database
                .lock()
                .map_err(|_| Error::DatabaseNotConnected);
            if let Ok(db) = db {
                db.add_share(share)
            } else {
                Err(Error::DatabaseLockError)
            }
        }

        pub fn get_shares_in_window(&self) -> Result<Vec<Share>, Error> {
            let shares = self.share_queue.get_shares_in_window();
            Ok(shares)
        }

        // Takes a payment in satoshis and returns a distribution in sats for the window and the fee for the pool
        pub fn get_distribution_for_window(
            &self,
            payment: u64,
            mut cached_users: HashMap<i64, String>,
        ) -> Result<(Vec<(String, u64)>, u64), Error> {
            // TODO: make math accurate/round down to the SAT-level

            let shares = self.get_shares_in_window()?;
            let shares_sum = shares.iter().map(|share| share.difficulty).sum::<f64>();
            let mut distribution: Vec<(String, u64)> = Vec::new();
            let mut total_fees: u64 = 0;
            let mut total_payments: u64 = 0;

            for share in shares {
                let share_fraction = share.difficulty / shares_sum;
                let share_payment = share_fraction * payment as f64;
                let fee_payment: u64 = (share.fee_rate * share_payment as f64) as u64;
                let remaining_payment = share_payment as u64 - fee_payment;

                if !cached_users.contains_key(&share.user_id) {
                    // Use memo for get_btc_address_for_user
                    let btc_address = self.get_btc_address_for_user(share.user_id)?;
                    cached_users.insert(share.user_id, btc_address);
                }

                let user_address = cached_users.get(&share.user_id).ok_or(Error::Unknown)?; // User should be in the cache
                distribution.push((user_address.clone(), remaining_payment));
                total_fees += fee_payment;
                total_payments += remaining_payment;
            }
            // Incase of rounding issues, pool absorbs the remainder
            let remainder = payment - (total_fees + total_payments);

            log::info!("Distribution: {:?}", distribution);
            log::info!("Total fees: {}", total_fees);
            log::info!("Total payments: {}", total_payments);
            log::info!("Remainder: {}", remainder);

            Ok((distribution, total_fees + remainder))
        }
    }
}
