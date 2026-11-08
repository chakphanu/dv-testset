/* SPDX-License-Identifier: MIT */
/* libcurl.so.4 stand-in for dstargateway (rig/Dockerfile.mmdvmstack). curl_easy_init fails, so the host file
 * downloader reports "failed to download" and the gateway keeps the host files it already has. Offline by
 * design: the rig has no internet. */
#include <stddef.h>

void *curl_easy_init(void)
{
    return NULL;
}

int curl_easy_setopt(void *handle, int option, ...)
{
    (void)handle;
    (void)option;
    return 2;   /* CURLE_FAILED_INIT */
}

int curl_easy_perform(void *handle)
{
    (void)handle;
    return 2;
}

void curl_easy_cleanup(void *handle)
{
    (void)handle;
}
