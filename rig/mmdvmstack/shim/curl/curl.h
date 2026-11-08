/* SPDX-License-Identifier: MIT */
/* The four libcurl entry points DStarGateway's host file downloader calls, nothing else. Written for the rig
 * (rig/Dockerfile.mmdvmstack): the downloader is never used (every hostfileUrl in the config is empty), so the
 * image carries the stub in rig/mmdvmstack/shim/curl-stub.c instead of libcurl and its twenty dependencies.
 * The option numbers are libcurl's; the stub ignores them. */
#ifndef DVT_SHIM_CURL_H
#define DVT_SHIM_CURL_H

#ifdef __cplusplus
extern "C" {
#endif

typedef void CURL;
typedef int CURLcode;
typedef int CURLoption;

#define CURLOPT_WRITEDATA 10001
#define CURLOPT_URL 10002
#define CURLOPT_WRITEFUNCTION 20011

CURL *curl_easy_init(void);
CURLcode curl_easy_setopt(CURL *handle, CURLoption option, ...);
CURLcode curl_easy_perform(CURL *handle);
void curl_easy_cleanup(CURL *handle);

#ifdef __cplusplus
}
#endif

#endif
