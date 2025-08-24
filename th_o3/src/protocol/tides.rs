use pyo3::prelude::*;
use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

const DB_FETCH_SIZE: usize = 100;

#[pymodule(submodule, name = "tides")]
pub mod tides {
    use super::*;

    #[pyclass]
    #[derive(Clone)]
    struct ShareIndex {
        pub index: usize,
    }

    #[pyclass]
    struct Database {
        pub connection: String,
    }

    impl Database {
        pub fn new(connection: String) -> Self {
            Self {
                connection,
            }
        }

        pub fn add_share(&self, share: Share) {
            // TODO: Implement
            // Make sure to keep index of the share
            println!("Adding share to database: {:?}", share.difficulty);
        }

        pub fn get_shares(&self, start_index: ShareIndex, max_count: usize) -> Vec<&Share> {
            // TODO: Implement
            // Get a maximum of max_count shares from the database
            println!("Getting shares from database: {:?}", start_index.index);
            vec![]
        }
    }

    #[pyclass]
    #[derive(Clone)]
    struct Share {
        pub difficulty: f64,          // difficulty of the share
        pub block_hash: String,       // block hash of the share
        pub nonce: Vec<u8>,           // nonce of the share
        pub timestamp: u64,           // timestamp of the share
        pub prevhash: Vec<u8>,        // prevhash of the share
        pub version: Vec<u8>,         // version of the share
        pub index: ShareIndex,             // index of the share in the database
        pub job_id: String,           // job id of the share
        pub job_version: Vec<u8>,     // job version of the share
        pub job_ntime: Vec<u8>,       // job ntime of the share
        pub job_nbits: Vec<u8>,       // job nbits of the share
        pub job_merkle_root: Vec<u8>, // job merkle root of the share
        pub job_coinb1: Vec<u8>,      // job coinb1 of the share
        pub job_coinb2: Vec<u8>,      // job coinb2 of the share
        pub job_extranonce1: Vec<u8>, // job extranonce1 of the share
        pub job_extranonce2: Vec<u8>, // job extranonce2 of the share
        pub diff_used: f64,           // difficulty used to calculate the share
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
                    curr += 1;
                } else { // The share needs to be split
                    let mut new_share = share.clone();
                    new_share.diff_used = to_add;
                    self.curr_sum += to_add;
                    self.shares.push_front(new_share);
                    // to_add is now 0.0
    
                    break;
                }
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
        pub fn new(share_log_window: u32, curr_difficulty: f64, share_buffer_size: usize, database: Arc<Mutex<Database>>) -> Self {
            Self {
                share_queue: ShareDeque::new(share_log_window, curr_difficulty),
                share_buffer: ShareBufferDeque::new(),
                share_buffer_size,
                curr_difficulty,
                share_log_window,
                database,
            }
        }

        pub fn add_share(&mut self, share: Share) {
            self.add_share_to_db(share.clone());

            let removed = self.share_queue.add_share(share);
            self.share_buffer.extend(removed);
            let _ = self.share_buffer.truncate(self.share_buffer_size);
        }

        pub fn adjust_difficulty(&mut self, difficulty: f64) {
            if difficulty == self.curr_difficulty {
                return;
            }

            let old_difficulty = self.curr_difficulty;
            self.curr_difficulty = difficulty;

            let removed = self.share_queue.adjust_difficulty(difficulty);
            self.share_buffer.extend(removed);
            let _ = self.share_buffer.truncate(self.share_buffer_size);

            if old_difficulty > difficulty {
                // Then grab some shares from the buffer
                let diff_to_add = self.share_queue.share_log_window() - self.share_buffer.curr_sum;

                // Check if we need to add more shares from the DB
                if diff_to_add > self.share_buffer.curr_sum { 

                    let mut overage = diff_to_add - self.share_buffer.curr_sum;
                    // TODO: grab more from the DB
                    if let Some(last_share) = self.share_buffer.last_share() {
                        let mut last_index = last_share.index;
                        let mut shares_from_db = Vec::new();
                        let db = self.database.lock();
                        if let Ok(db) = db {
                            while overage > 0.0 {
                                let shares_ = db.get_shares(last_index.clone(), DB_FETCH_SIZE);
                                for share in shares_ {
                                    shares_from_db.push(share.clone());
                                    last_index = share.index.clone();
                                    overage -= share.difficulty;
                                }
                            }
                        } else {
                            println!("Failed to lock database");
                        }

                        // Add the shares from the DB to the buffer
                        self.share_buffer.extend(shares_from_db.clone());
                    }
                }

                // Add shares from the buffer to the queue until the queue is full or the buffer is empty
                while self.share_queue.curr_sum < self.share_queue.share_log_window() {
                    let Some(share) = self.share_buffer.pop_back() else {
                        break;
                    };
                    self.share_queue.push_front_shares(vec![share]);
                }
            }
        }

        fn add_share_to_db(&mut self, share: Share) {
            let db = self.database.lock();
            if let Ok(db) = db {
                db.add_share(share);
            } else {
                println!("Failed to lock database");
            }
        }
    }
}
