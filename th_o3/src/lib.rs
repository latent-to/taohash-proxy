use pyo3::{prelude::*, wrap_pymodule};

pub mod protocol;

#[pymodule]
fn th_o3(m: &Bound<'_, PyModule>) -> PyResult<()> {
    register_child_module(m)?;
    Ok(())
}

fn register_child_module(parent_module: &Bound<'_, PyModule>) -> PyResult<()> {
    let protocol_module = PyModule::new(parent_module.py(), "protocol")?;
    protocol_module.add_wrapped(wrap_pymodule!(protocol::difficulty::difficulty))?;
    protocol_module.add_wrapped(wrap_pymodule!(protocol::tides::tides))?;
    parent_module.add_submodule(&protocol_module)
}
