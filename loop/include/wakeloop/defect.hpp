// The variants of the loop: the fixed loop and six injectable wake defects.
// See design/v2-defects.md, section 4, for each defect's sites on each backend.
#pragma once

#include <array>
#include <optional>
#include <string_view>

namespace wakeloop
{

	enum class Defect
	{
		none,                  // the fixed loop
		absent_wake,           // the post does not signal
		unobserved_primitive,  // the post signals an object the wait does not watch
		lazy_wake_race,        // the wake decision comes from a look at the stack made before the push
		wrong_worker,          // the push goes to worker i, the wake to worker (i + 1) mod W
		no_retrigger,          // the wake registration is one-shot and never re-armed
		undrained_edge,        // the worker runs at most kUndrainedBudget tasks per pass, then parks
	};

	inline constexpr std::array<Defect, 7> kAllVariants{
		Defect::none,         Defect::absent_wake,  Defect::unobserved_primitive, Defect::lazy_wake_race,
		Defect::wrong_worker, Defect::no_retrigger, Defect::undrained_edge,
	};

	/// Tasks an undrained-edge worker runs per pass.
	inline constexpr unsigned kUndrainedBudget = 64;

	constexpr std::string_view defect_name(Defect d) noexcept
	{
		switch (d)
		{
			case Defect::none: return "none";
			case Defect::absent_wake: return "absent_wake";
			case Defect::unobserved_primitive: return "unobserved_primitive";
			case Defect::lazy_wake_race: return "lazy_wake_race";
			case Defect::wrong_worker: return "wrong_worker";
			case Defect::no_retrigger: return "no_retrigger";
			case Defect::undrained_edge: return "undrained_edge";
		}
		return "?";
	}

	constexpr std::optional<Defect> parse_defect(std::string_view name) noexcept
	{
		for (Defect d : kAllVariants)
		{
			if (defect_name(d) == name) return d;
		}
		return std::nullopt;
	}

}  // namespace wakeloop
