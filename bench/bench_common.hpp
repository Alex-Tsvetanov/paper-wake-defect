// Helpers that wakeprobe and abpair share: JSON text and the lists of backends and variants.
#pragma once

#include "wakeloop/variants.hpp"

#include <cstdio>
#include <string>
#include <string_view>

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

}  // namespace benchutil
