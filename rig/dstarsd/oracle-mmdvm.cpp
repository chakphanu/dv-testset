// SPDX-License-Identifier: MIT
// Driver for MMDVMHost's CDStarSlowData (GPL-2.0-or-later, linked unmodified at image
// build time, run as a process; docs/dstar-slowdata-cases.md). Reads frames from stdin,
// calls the class the way MMDVMHost's DStarControl.cpp does on its network path (a sync
// frame -> start(), any other frame -> add(), then getText() after every frame that is
// not the sync) and prints what the class decided.
//
// Input lines:   B <name>                begin an over: new decoder state
//                F <seq> <hex6>          one frame: sequence 0..20, the 3 slow data bytes
//                                        as sent (frame 0 is the sync pattern)
//                E                       end of the over
// Output lines:  R <kind> <seq> <hex>    kind: text (20 bytes), block (type byte of a
//                                        completed 6 byte block), header (41 bytes)
//                D <name>               echo of the over name
#include "DStarSlowData.h"
#include "DStarHeader.h"
#include "DStarDefines.h"

#include <cstdarg>
#include <cstdio>
#include <cstring>

void Log(unsigned int level, const char* fmt, ...)
{
	std::va_list ap;
	va_start(ap, fmt);
	std::fprintf(stderr, "[log %u] ", level);
	std::vfprintf(stderr, fmt, ap);
	std::fprintf(stderr, "\n");
	va_end(ap);
}

static void printHex(const char* kind, unsigned int seq, const unsigned char* data, unsigned int len)
{
	std::printf("R %s %u ", kind, seq);
	for (unsigned int i = 0U; i < len; i++)
		std::printf("%02x", data[i]);
	std::printf("\n");
}

int main()
{
	CDStarSlowData sd;
	bool headerSeen = false;
	char line[256];

	while (std::fgets(line, sizeof(line), stdin) != nullptr) {
		if (line[0] == 'B') {
			sd.start();
			headerSeen = false;
			std::printf("D %s", line + 2);
		} else if (line[0] == 'F') {
			unsigned int seq = 0U, a = 0U, b = 0U, c = 0U;
			if (std::sscanf(line, "F %u %2x%2x%2x", &seq, &a, &b, &c) != 4)
				continue;

			unsigned char frame[12U];
			std::memset(frame, 0x00, sizeof(frame));
			frame[9U]  = (unsigned char)a;
			frame[10U] = (unsigned char)b;
			frame[11U] = (unsigned char)c;

			if (seq == 0U) {
				sd.start();
				headerSeen = false;
				continue;
			}

			sd.add(frame);

			if (sd.isComplete()) {
				unsigned char type = sd.getType(true);
				printHex("block", seq, &type, 1U);

				if ((type & DSTAR_SLOW_DATA_TYPE_MASK) == DSTAR_SLOW_DATA_TYPE_HEADER && !headerSeen) {
					CDStarHeader* header = sd.getHeader();
					if (header != nullptr) {
						unsigned char raw[DSTAR_HEADER_LENGTH_BYTES];
						header->get(raw);
						printHex("header", seq, raw, DSTAR_HEADER_LENGTH_BYTES);
						delete header;
						headerSeen = true;
					}
				}
			}

			const unsigned char* text = sd.getText();
			if (text != nullptr)
				printHex("text", seq, text, 20U);
		} else if (line[0] == 'E') {
			std::printf("D end\n");
		}
		std::fflush(stdout);
	}
	return 0;
}
