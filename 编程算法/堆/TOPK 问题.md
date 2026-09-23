


## 具体解法

```java
package com.taoes.code.heap;

import org.junit.Assert;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

public class TopK {
    public static void main(String[] args) {
        Solution s = new Solution();
        Assert.assertEquals(4, s.maxKth2(new int[]{5, 2, 1, 4, 3}, 2));
        Assert.assertEquals(3, s.maxKth2(new int[]{5, 2, 1, 4, 3}, 3));

        Assert.assertEquals(4, s.maxKth1(new Integer[]{5, 2, 1, 4, 3}, 2));
        Assert.assertEquals(3, s.maxKth1(new Integer[]{5, 2, 1, 4, 3}, 3));


        Assert.assertArrayEquals(new Integer[]{3, 4, 5}, s.topK1(new Integer[]{5, 2, 1, 4, 3}, 3));
        Assert.assertArrayEquals(new Integer[]{4, 5}, s.topK1(new Integer[]{5, 2, 1, 4, 3}, 2));


        Assert.assertArrayEquals(new Integer[]{3, 4, 5}, s.topK2(new Integer[]{5, 2, 1, 4, 3}, 3));
        Assert.assertArrayEquals(new Integer[]{4, 5}, s.topK2(new Integer[]{5, 2, 1, 4, 3}, 2));
    }

    static class Solution {

        // 快速选择
        public int maxKth1(Integer[] nums, int k) {
            while (nums.length > 0) {
                int p = nums[nums.length - 1];
                List<Integer> g = new ArrayList<>();
                List<Integer> e = new ArrayList<>();
                List<Integer> l = new ArrayList<>();
                for (Integer num : nums) {
                    if (num > p) {
                        g.add(num);
                    } else if (num < p) {
                        l.add(num);
                    } else {
                        e.add(num);
                    }
                }

                // 检查 K 的位置
                if (k <= g.size()) {
                    nums = g.toArray(new Integer[0]);
                } else if (k <= g.size() + e.size()) {
                    return e.get(0);
                } else {
                    nums = l.toArray(new Integer[0]);
                }
            }
            return -1;
        }


        //小顶堆
        public int maxKth2(int[] nums, int k) {
            Integer[] heap = new Integer[k];
            for (int i = 0; i < nums.length; i++) {
                // 当前堆还没有满
                if (i < k) {
                    heap[i] = nums[i];
                    if (i == k - 1) {
                        for (int j = k / 2 - 1; j >= 0; j--) {
                            heapifyMin(heap, j, k);
                        }

                    }
                    continue;
                }


                if (nums[i] > heap[0]) {
                    heap[0] = nums[i];
                    this.heapifyMin(heap, 0, k);
                }
            }
            return heap[0];
        }

        public void heapifyMin(Integer[] heap, int i, int len) {
            int min = i;
            int leftIndex = 2 * i + 1;
            int rightIndex = 2 * i + 2;

            if (leftIndex < len && heap[leftIndex] < heap[min]) {
                min = leftIndex;
            }

            if (rightIndex < len && heap[rightIndex] < heap[min]) {
                min = rightIndex;
            }

            if (min != i) {
                swap(heap, i, min);
                this.heapifyMin(heap, min, i);
            }
        }

        private void swap(Integer[] heap, int i, int min) {
            int t = heap[i];
            heap[i] = heap[min];
            heap[min] = t;
        }


        //  小顶堆
        public Integer[] topK1(Integer[] nums, int k) {
            Integer[] heap = new Integer[k];
            for (int i = 0; i < nums.length; i++) {
                // 当前堆还没有满
                if (i < k) {
                    heap[i] = nums[i];
                    if (i == k - 1) {
                        for (int j = k / 2 - 1; j >= 0; j--) {
                            heapifyMin(heap, j, k);
                        }

                    }
                    continue;
                }


                if (nums[i] > heap[0]) {
                    heap[0] = nums[i];
                    this.heapifyMin(heap, 0, k);
                }
            }
            return heap;
        }

        //  排序算法
        public Integer[] topK2(Integer[] nums, int k) {
            Arrays.sort(nums);
            Integer[] topk = new Integer[k];
            int index = 0;
            for (int i = nums.length - k; i < nums.length; i++) {
                topk[index++] = nums[i];
            }
            return topk;
        }
    }
}


```