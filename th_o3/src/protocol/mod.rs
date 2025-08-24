use pyo3::prelude::*;
use pyo3::wrap_pymodule;

pub mod difficulty;

#[pyfunction(name = "__t")]
pub fn py_test_fn() -> PyResult<()> {
    println!("test");
    Ok(())
}
