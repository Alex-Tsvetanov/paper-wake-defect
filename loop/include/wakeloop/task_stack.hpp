// The queue shared by every backend: a lock-free stack of caller-owned tasks.
//
// Posters push with one compare-and-swap. The worker takes the whole stack with one exchange
// and reverses it, so tasks from one poster run in the order they were posted. A push reports
// whether it found the stack empty: only that push needs to wake the worker (see
// design/minimal-loop.md, section 2.3, for why this loses no wake).
#pragma once

#include <atomic>

namespace wakeloop
{

	/// A unit of work, owned by the caller. The loop links it through `next` while it is queued
	/// and calls `run` once, on the worker thread. It must stay alive until it has run, or until
	/// the loop that holds it is destroyed.
	struct Task
	{
		Task* next = nullptr;
		void (*run)(Task&) noexcept = nullptr;
	};

	namespace detail
	{

		class TaskStack
		{
		public:
			/// Pushes `task`. True when the stack was empty before the push. Release order on
			/// success publishes the task's contents to the worker's acquire in take_all().
			bool push(Task& task) noexcept
			{
				Task* head = head_.load(std::memory_order_relaxed);
				do
				{
					task.next = head;
				} while (!head_.compare_exchange_weak(head, &task, std::memory_order_release, std::memory_order_relaxed));
				return head == nullptr;
			}

			/// Takes every queued task, oldest first. Worker thread only.
			Task* take_all() noexcept
			{
				Task* newest_first = head_.exchange(nullptr, std::memory_order_acquire);
				Task* oldest_first = nullptr;
				while (newest_first != nullptr)
				{
					Task* next = newest_first->next;
					newest_first->next = oldest_first;
					oldest_first = newest_first;
					newest_first = next;
				}
				return oldest_first;
			}

		private:
			std::atomic<Task*> head_{nullptr};
		};

		/// Runs a list from take_all(). Each task's link is read before the task runs, because a
		/// task may destroy itself or post itself again.
		inline void run_all(Task* task) noexcept
		{
			while (task != nullptr)
			{
				Task* next = task->next;
				task->run(*task);
				task = next;
			}
		}

	}  // namespace detail

}  // namespace wakeloop
