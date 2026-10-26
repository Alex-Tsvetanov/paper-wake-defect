// Condition-variable backend, on both platforms. See design/v2-defects.md, section 4.
//
// The wake object is a predicate (pending_) guarded by a mutex. A wake sets it under the mutex
// and then notifies; the worker waits for it with the bound and clears it under the mutex after
// every return. Because the predicate is set under the mutex, a notify that comes before the
// worker sleeps is never lost: the worker reads the predicate before it sleeps. Spurious returns
// re-wait for the rest of the bound (wait_for with a predicate). Stop sets its own predicate,
// which stays set, so every later wait returns at once.
//
// Defect site: unobserved primitive sets a second predicate and notifies a second condition
// variable, which the wait never reads. No re-trigger has no site: a notify needs no registration.
#include "wakeloop/backends.hpp"

#include "common.hpp"

#include <chrono>
#include <exception>
#include <mutex>

namespace wakeloop
{

	template <Defect D>
	CondvarBackend<D>::CondvarBackend(WaitSpec spec) : wait_us_(detail::checked_bound(spec.wait_us)), poll_(spec.poll)
	{
	}

	template <Defect D>
	void CondvarBackend<D>::wait()
	{
		std::unique_lock lock(m_);
		const auto ready = [this] { return pending_ || stopped_; };
		if (poll_)
		{
			// Returns at once; the predicate is still read and cleared under the mutex.
		}
		else if (wait_us_ == 0)
		{
			cv_.wait(lock, ready);
		}
		else
		{
			cv_.wait_for(lock, std::chrono::microseconds(wait_us_), ready);
		}
		pending_ = false;
	}

	/// One non-blocking poll (the widened lazy-wake plant's window): reads the predicate under the
	/// mutex and leaves it set, so the next wait still sees it.
	template <Defect D>
	void CondvarBackend<D>::poll_nowait()
	{
		std::lock_guard lock(m_);
		[[maybe_unused]] const volatile bool seen = pending_ || stopped_;
	}

	template <Defect D>
	void CondvarBackend<D>::wake() noexcept
	{
		try
		{
			if constexpr (D == Defect::unobserved_primitive)
			{
				{
					std::lock_guard guard(spare_m_);
					spare_pending_ = true;
				}
				spare_cv_.notify_one();
			}
			else
			{
				{
					std::lock_guard guard(m_);
					pending_ = true;
				}
				cv_.notify_one();
			}
		}
		catch (const std::exception&)
		{
			detail::fatal("condvar: locking the wake mutex failed", 0);
		}
	}

	template <Defect D>
	void CondvarBackend<D>::stop() noexcept
	{
		try
		{
			{
				std::lock_guard guard(m_);
				stopped_ = true;
			}
			cv_.notify_all();
		}
		catch (const std::exception&)
		{
			detail::fatal("condvar: locking the wake mutex for stop failed", 0);
		}
	}

	template <Defect D>
	const char* CondvarBackend<D>::wait_method() const noexcept
	{
		return "std::condition_variable::wait_for";
	}

	WAKELOOP_INSTANTIATE_ALL(CondvarBackend)

}  // namespace wakeloop
