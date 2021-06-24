# All rights reserved.

# Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

# 1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.

# 2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.

# 3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.

# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
from random import sample
import torch
from torch.utils.data import DataLoader


# https://github.com/NVIDIA/apex/blob/master/examples/imagenet/main_amp.py
class Prefetcher:
    def __init__(self, loader: DataLoader, rank: int, transform = None):
        self._rank = rank
        self._loader = loader
        self._iter = iter(loader)
        self._stream = torch.cuda.Stream(self._rank)
        self._nextSample = None
        self._transform = transform
        self._exhausted = False

    def __iter__(self):
        self._exhausted = False
        self._iter = iter(self._loader)
        return self

    def __next__(self):
        torch.cuda.current_stream(self._rank).wait_stream(self._stream)
        sample = self._nextSample
        if sample is not None:
            sample.record_stream(torch.cuda.current_stream())
        else:
            if self._exhausted:
                raise StopIteration
            else:
                self._preLoad()
                torch.cuda.current_stream(self._rank).wait_stream(self._stream)
                sample = self._nextSample
                sample.record_stream(torch.cuda.current_stream())
        self._preLoad()
        return sample

    def _preLoad(self):
        try:
            sample = next(self._iter)
            with torch.cuda.stream(self._stream):
                sample = sample.to(self._rank, non_blocking=True)
                if self._transform is not None:
                    sample = self._transform(sample)
                self._nextSample = sample
        except StopIteration:
            self._nextSample = None
            self._exhausted = True
