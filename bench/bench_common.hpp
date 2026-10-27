// Helpers that wakeprobe and abpair share: JSON text and the lists of backends and variants.
#pragma once

#include "wakeloop/variants.hpp"

#include <charconv>
#include <cstdio>
#include <string>
#include <string_view>
#include <system_error>
#include <vector>

namespace benchutil
{

	/// `s` as the inside of a JSON string.
	inline std::string json_escape(std::string_view s)
	{
		std::string out;
		for (const char c : s)
		{
			switch (c)
			{
				case '"': out += "\\\""; break;
				case '\\': out += "\\\\"; break;
				case '\n': out += "\\n"; break;
				case '\r': out += "\\r"; break;
				case '\t': out += "\\t"; break;
				default:
					if (static_cast<unsigned char>(c) < 0x20)
					{
						char buf[8];
						std::snprintf(buf, sizeof(buf), "\\u%04x", static_cast<unsigned>(c));
						out += buf;
					}
					else
					{
						out += c;
					}
			}
		}
		return out;
	}

	/// The backends of this platform, as "a|b|c".
	inline std::string backend_list()
	{
		std::string s;
		for (const std::string_view b : wakeloop::kBackends)
		{
			if (!s.empty()) s += '|';
			s += b;
		}
		return s;
	}

	/// Every variant name, as "a|b|c".
	inline std::string variant_list()
	{
		std::string s;
		for (const wakeloop::Defect d : wakeloop::kAllVariants)
		{
			if (!s.empty()) s += '|';
			s += wakeloop::defect_name(d);
		}
		return s;
	}

	/// A comma-separated list of logical CPU numbers ("8,10,12,14"). Empty when the text is not
	/// such a list; the caller reports it.
	inline std::vector<unsigned> parse_cpu_list(std::string_view text)
	{
		std::vector<unsigned> cpus;
		for (;;)
		{
			const auto comma = text.find(',');
			const std::string_view item = text.substr(0, comma);
			unsigned v = 0;
			const auto [end, ec] = std::from_chars(item.data(), item.data() + item.size(), v);
			if (item.empty() || ec != std::errc{} || end != item.data() + item.size()) return {};
			cpus.push_back(v);
			if (comma == std::string_view::npos) return cpus;
			text.remove_prefix(comma + 1);
		}
	}

	/// A JSON array of CPU numbers, or null when the list is empty.
	template <class T>
	std::string cpu_list_json(const std::vector<T>& cpus)
	{
		if (cpus.empty()) return "null";
		std::string s = "[";
		for (std::size_t i = 0; i < cpus.size(); ++i) s += (i ? "," : "") + std::to_string(cpus[i]);
		return s + "]";
	}

}  // namespace benchutil
