some tests are in git (small ones), but big tests files are not in git, you must generat or download them so use _prepare_test_files.sh to generate them, they will contain autogen_ prefix so you can delete/regenerate them when wanted

then those tests can be used to do manual compares with differ2

each test have file a and file b, which are supposed to be used in one compare

some interesting test files are:
  - autogen_test_8_blocks__Myers_Histogram_Poison , this file is only 150kb but it takes more time than even 50mb files, it shows how complexity is what affects compare speed not file size
  - autogen_140mb , this can be used to benshmark ram usage and also speed for very big files


________________________________________________________

annex\memory_leak_diagnostics folder contain some useful scripts to diagnostic memory leak when python and fpc heaptrc fail to find a leak, this happen when fpc retain memory for reuse as explained here https://github.com/Alexey-T/CudaText/issues/6533