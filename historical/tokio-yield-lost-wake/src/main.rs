// Historical missed wake in Tokio.
//
// A worker is inside the multi-thread runtime, then block_in_place leaves the
// runtime entered-state while the scheduler context remains. yield_now is then
// polled by futures_executor::block_on. At the buggy commit the wake is deferred
// onto that scheduler and the task does not continue. The fix defers a wake only
// while the runtime is entered, and the task continues.
//
// This is the shape of the test added in the fix. It is not the paper's latency
// matrix, and it does not estimate power or a false-positive rate.
//
// buggy/ pins the release whose context.rs is the pre-fix blob.
// fixed/ pins the release whose context.rs is the post-fix blob.

use std::sync::LazyLock;
use tokio::runtime::Runtime;

static GLOBAL: LazyLock<Runtime> = LazyLock::new(|| {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .unwrap()
});

async fn task() {
    println!("start");
    tokio::task::yield_now().await;
    println!("done");
}

fn main() {
    GLOBAL.block_on(async {
        let joined = tokio::spawn(async {
            tokio::task::block_in_place(|| {
                futures_executor::block_on(task());
            });
        });
        joined.await.unwrap();
    });
    println!("finished");
}
