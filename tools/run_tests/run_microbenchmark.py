#!/usr/bin/env python2.7
# Copyright 2017, Google Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
#     * Redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above
# copyright notice, this list of conditions and the following disclaimer
# in the documentation and/or other materials provided with the
# distribution.
#     * Neither the name of Google Inc. nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import cgi
import multiprocessing
import os
import subprocess
import sys
import argparse

import python_utils.jobset as jobset
import python_utils.start_port_server as start_port_server

flamegraph_dir = os.path.join(os.path.expanduser('~'), 'FlameGraph')

os.chdir(os.path.join(os.path.dirname(sys.argv[0]), '../..'))
if not os.path.exists('reports'):
  os.makedirs('reports')

port_server_port = 32766
start_port_server.start_port_server(port_server_port)

def fnize(s):
  out = ''
  for c in s:
    if c in '<>, /':
      if len(out) and out[-1] == '_': continue
      out += '_'
    else:
      out += c
  return out

# index html
index_html = """
<html>
<head>
<title>Microbenchmark Results</title>
</head>
<body>
"""

def heading(name):
  global index_html
  index_html += "<h1>%s</h1>\n" % name

def link(txt, tgt):
  global index_html
  index_html += "<p><a href=\"%s\">%s</a></p>\n" % (
      cgi.escape(tgt, quote=True), cgi.escape(txt))

def text(txt):
  global index_html
  index_html += "<p><pre>%s</pre></p>\n" % cgi.escape(txt)

def collect_latency(bm_name, args):
  """generate latency profiles"""
  benchmarks = []
  profile_analysis = []
  cleanup = []

  heading('Latency Profiles: %s' % bm_name)
  subprocess.check_call(
      ['make', bm_name,
       'CONFIG=basicprof', '-j', '%d' % multiprocessing.cpu_count()])
  for line in subprocess.check_output(['bins/basicprof/%s' % bm_name,
                                       '--benchmark_list_tests']).splitlines():
    link(line, '%s.txt' % fnize(line))
    benchmarks.append(
        jobset.JobSpec(['bins/basicprof/%s' % bm_name,
                        '--benchmark_filter=^%s$' % line,
                        '--benchmark_min_time=0.05'],
                       environ={'LATENCY_TRACE': '%s.trace' % fnize(line)}))
    profile_analysis.append(
        jobset.JobSpec([sys.executable,
                        'tools/profiling/latency_profile/profile_analyzer.py',
                        '--source', '%s.trace' % fnize(line), '--fmt', 'simple',
                        '--out', 'reports/%s.txt' % fnize(line)], timeout_seconds=None))
    cleanup.append(jobset.JobSpec(['rm', '%s.trace' % fnize(line)]))
    # periodically flush out the list of jobs: profile_analysis jobs at least
    # consume upwards of five gigabytes of ram in some cases, and so analysing
    # hundreds of them at once is impractical -- but we want at least some
    # concurrency or the work takes too long
    if len(benchmarks) >= min(16, multiprocessing.cpu_count()):
      # run up to half the cpu count: each benchmark can use up to two cores
      # (one for the microbenchmark, one for the data flush)
      jobset.run(benchmarks, maxjobs=max(1, multiprocessing.cpu_count()/2),
                 add_env={'GRPC_TEST_PORT_SERVER': 'localhost:%d' % port_server_port})
      jobset.run(profile_analysis, maxjobs=multiprocessing.cpu_count())
      jobset.run(cleanup, maxjobs=multiprocessing.cpu_count())
      benchmarks = []
      profile_analysis = []
      cleanup = []
  # run the remaining benchmarks that weren't flushed
  if len(benchmarks):
    jobset.run(benchmarks, maxjobs=max(1, multiprocessing.cpu_count()/2),
               add_env={'GRPC_TEST_PORT_SERVER': 'localhost:%d' % port_server_port})
    jobset.run(profile_analysis, maxjobs=multiprocessing.cpu_count())
    jobset.run(cleanup, maxjobs=multiprocessing.cpu_count())

def collect_perf(bm_name, args):
  """generate flamegraphs"""
  heading('Flamegraphs: %s' % bm_name)
  subprocess.check_call(
      ['make', bm_name,
       'CONFIG=mutrace', '-j', '%d' % multiprocessing.cpu_count()])
  benchmarks = []
  profile_analysis = []
  cleanup = []
  for line in subprocess.check_output(['bins/mutrace/%s' % bm_name,
                                       '--benchmark_list_tests']).splitlines():
    link(line, '%s.svg' % fnize(line))
    benchmarks.append(
        jobset.JobSpec(['perf', 'record', '-o', '%s-perf.data' % fnize(line),
                        '-g', '-F', '997',
                        'bins/mutrace/%s' % bm_name,
                        '--benchmark_filter=^%s$' % line,
                        '--benchmark_min_time=10']))
    profile_analysis.append(
        jobset.JobSpec(['tools/run_tests/performance/process_local_perf_flamegraphs.sh'],
                       environ = {
                           'PERF_BASE_NAME': fnize(line),
                           'OUTPUT_DIR': 'reports',
                           'OUTPUT_FILENAME': fnize(line),
                       }))
    cleanup.append(jobset.JobSpec(['rm', '%s-perf.data' % fnize(line)]))
    cleanup.append(jobset.JobSpec(['rm', '%s-out.perf' % fnize(line)]))
    # periodically flush out the list of jobs: temporary space required for this
    # processing is large
    if len(benchmarks) >= 20:
      # run up to half the cpu count: each benchmark can use up to two cores
      # (one for the microbenchmark, one for the data flush)
      jobset.run(benchmarks, maxjobs=1,
                 add_env={'GRPC_TEST_PORT_SERVER': 'localhost:%d' % port_server_port})
      jobset.run(profile_analysis, maxjobs=multiprocessing.cpu_count())
      jobset.run(cleanup, maxjobs=multiprocessing.cpu_count())
      benchmarks = []
      profile_analysis = []
      cleanup = []
  # run the remaining benchmarks that weren't flushed
  if len(benchmarks):
    jobset.run(benchmarks, maxjobs=1,
               add_env={'GRPC_TEST_PORT_SERVER': 'localhost:%d' % port_server_port})
    jobset.run(profile_analysis, maxjobs=multiprocessing.cpu_count())
    jobset.run(cleanup, maxjobs=multiprocessing.cpu_count())

def collect_summary(bm_name, args):
  heading('Summary: %s' % bm_name)
  subprocess.check_call(
      ['make', bm_name,
       'CONFIG=counters', '-j', '%d' % multiprocessing.cpu_count()])
  cmd = ['bins/counters/%s' % bm_name,
         '--benchmark_out=out.json',
         '--benchmark_out_format=json']
  if args.summary_time is not None:
    cmd += ['--benchmark_min_time=%d' % args.summary_time]
  text(subprocess.check_output(cmd))
  if args.bigquery_upload:
    with open('out.csv', 'w') as f:
      f.write(subprocess.check_output(['tools/profiling/microbenchmarks/bm2bq.py', 'out.json']))
    subprocess.check_call(['bq', 'load', 'microbenchmarks.microbenchmarks', 'out.csv'])

collectors = {
  'latency': collect_latency,
  'perf': collect_perf,
  'summary': collect_summary,
}

argp = argparse.ArgumentParser(description='Collect data from microbenchmarks')
argp.add_argument('-c', '--collect',
                  choices=sorted(collectors.keys()),
                  nargs='+',
                  default=sorted(collectors.keys()),
                  help='Which collectors should be run against each benchmark')
argp.add_argument('-b', '--benchmarks',
                  default=['bm_fullstack', 'bm_closure', 'bm_cq', 'bm_call_create', 'bm_error'],
                  nargs='+',
                  type=str,
                  help='Which microbenchmarks should be run')
argp.add_argument('--bigquery_upload',
                  default=False,
                  action='store_const',
                  const=True,
                  help='Upload results from summary collection to bigquery')
argp.add_argument('--summary_time',
                  default=None,
                  type=int,
                  help='Minimum time to run benchmarks for the summary collection')
args = argp.parse_args()

for bm_name in args.benchmarks:
  for collect in args.collect:
    collectors[collect](bm_name, args)

index_html += "</body>\n</html>\n"
with open('reports/index.html', 'w') as f:
  f.write(index_html)
