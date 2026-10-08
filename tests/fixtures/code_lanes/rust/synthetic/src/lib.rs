//! A trait, its implementation, a declarative macro and an async function.

pub trait Sink {
    fn write(&mut self, line: &str) -> usize;

    fn flush(&mut self) {}
}

pub struct Buffer {
    lines: Vec<String>,
}

impl Sink for Buffer {
    fn write(&mut self, line: &str) -> usize {
        self.lines.push(line.to_string());
        self.lines.len()
    }
}

macro_rules! emit {
    ($sink:expr, $line:expr) => {
        $sink.write($line)
    };
}

pub async fn drain(buffer: &mut Buffer) -> usize {
    emit!(buffer, "done");
    buffer.write("tail")
}
