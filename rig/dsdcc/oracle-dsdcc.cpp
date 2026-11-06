// SPDX-License-Identifier: MIT
// Driver for DSDcc's D-STAR decoder (GPL-3.0-or-later, built unmodified into the image and linked as a
// shared library, run as a process; docs/dstar-slowdata-cases.md, stage 2). It reads S16LE discriminator
// samples (48 kS/s, mono) from stdin, feeds them to DSDcc::DSDDecoder in D-STAR-only mode (what the stock
// tool does for -fd) and prints every CHANGE of the state the public accessors expose: the 20 character
// info text, the locator, and which of the four header strings (MY, UR, RPT1, RPT2) are non-empty, plus
// two content-free quality flags (all non-empty header strings are printable ASCII; the locator has the
// shape of a six character Maidenhead locator).
// The stock tool dsdccx cannot be sampled faster than every 0.1 s (its -m option ignores smaller values),
// so one status line per five voice frames is too coarse to see which frame changed what.
//
// Output lines:  R state <sample index> <text hex, 20 bytes> <locator hex or ->  <header flags 0..15>
//                D end <samples read>
// The header strings themselves are never printed (they are personal data in a real recording).
#include "dsd_decoder.h"

#include <cstdio>
#include <cstring>
#include <string>

static bool printable(const std::string& s)
{
	for (unsigned char ch : s)
		if (ch < 32U || ch > 126U)
			return false;
	return true;
}

static bool maidenhead(const std::string& s)
{
	return s.size() == 6U && s[0] >= 'A' && s[0] <= 'R' && s[1] >= 'A' && s[1] <= 'R'
	    && s[2] >= '0' && s[2] <= '9' && s[3] >= '0' && s[3] <= '9'
	    && s[4] >= 'A' && s[4] <= 'X' && s[5] >= 'A' && s[5] <= 'X';
}

int main()
{
	DSDcc::DSDDecoder decoder;
	decoder.setQuiet();
	decoder.setDecodeMode(DSDcc::DSDDecoder::DSDDecodeDStar, true);
	decoder.enableMbelib(false);
	decoder.setMyPoint(0.0f, 0.0f);

	std::string lastText, lastLoc;
	int lastFlags = -1;
	unsigned long long index = 0ULL;
	short buf[4096];
	size_t got;

	while ((got = std::fread(buf, sizeof(short), sizeof(buf) / sizeof(short), stdin)) > 0U) {
		for (size_t i = 0U; i < got; i++, index++) {
			decoder.run(buf[i]);

			const DSDcc::DSDDstar& dstar = decoder.getDStarDecoder();
			std::string text(dstar.getInfoText(), 20U);
			std::string loc(dstar.getLocator(), strnlen(dstar.getLocator(), 6U));
			int flags = (dstar.getMySign().empty() ? 0 : 1) | (dstar.getYourSign().empty() ? 0 : 2)
			          | (dstar.getRpt1().empty() ? 0 : 4) | (dstar.getRpt2().empty() ? 0 : 8);
			if (printable(dstar.getMySign()) && printable(dstar.getYourSign()) && printable(dstar.getRpt1())
			    && printable(dstar.getRpt2()))
				flags |= 16;
			if (maidenhead(loc))
				flags |= 32;
			if (text == lastText && loc == lastLoc && flags == lastFlags)
				continue;
			lastText = text;
			lastLoc = loc;
			lastFlags = flags;

			std::printf("R state %llu ", index);
			for (unsigned char ch : text)
				std::printf("%02x", ch);
			std::printf(" ");
			if (loc.empty())
				std::printf("-");
			for (unsigned char ch : loc)
				std::printf("%02x", ch);
			std::printf(" %d\n", flags);
		}
	}
	std::printf("D end %llu\n", index);
	return 0;
}
