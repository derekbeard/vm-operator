// © Broadcom. All Rights Reserved.
// The term "Broadcom" refers to Broadcom Inc. and/or its subsidiaries.
// SPDX-License-Identifier: Apache-2.0

// Command vm-metrics-adapter serves VM utilization totals for
// VirtualMachineReplicaSets through custom.metrics.k8s.io. It is stateless and
// read-only, and runs separately from the VM Operator manager.
package main

import (
	"context"
	"os"
	"os/signal"
	"syscall"

	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/component-base/logs"
	"k8s.io/klog/v2"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	basecmd "sigs.k8s.io/custom-metrics-apiserver/pkg/cmd"

	vmopv1 "github.com/vmware-tanzu/vm-operator/api/v1alpha6"
	"github.com/vmware-tanzu/vm-operator/pkg/metricsadapter"
)

type adapter struct {
	basecmd.AdapterBase

	minCoverage float64
}

func main() {
	logs.InitLogs()
	defer logs.FlushLogs()

	a := &adapter{}
	a.Name = "vm-metrics-adapter"
	a.Flags().Float64Var(&a.minCoverage, "min-coverage", metricsadapter.DefaultMinCoverage,
		"Fraction (0..1) of a ReplicaSet's VMs that must have a fresh utilization sample "+
			"before a total is reported; below it the metric is unavailable and the HPA holds.")
	logs.AddFlags(a.Flags())
	if err := a.Flags().Parse(os.Args); err != nil {
		klog.Fatalf("unable to parse flags: %v", err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	cfg, err := a.ClientConfig()
	if err != nil {
		klog.Fatalf("unable to build client config: %v", err)
	}

	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		klog.Fatalf("unable to register core types: %v", err)
	}
	if err := vmopv1.AddToScheme(scheme); err != nil {
		klog.Fatalf("unable to register VM Operator types: %v", err)
	}

	// An informer-backed cache keeps HPA polling from becoming API server load.
	c, err := cache.New(cfg, cache.Options{Scheme: scheme})
	if err != nil {
		klog.Fatalf("unable to create cache: %v", err)
	}
	go func() {
		if err := c.Start(ctx); err != nil {
			klog.Fatalf("cache stopped: %v", err)
		}
	}()
	// Register the informers before waiting, otherwise sync has nothing to wait on.
	for _, obj := range []client.Object{
		&vmopv1.VirtualMachine{}, &vmopv1.VirtualMachineReplicaSet{}} {

		if _, err := c.GetInformer(ctx, obj); err != nil {
			klog.Fatalf("unable to start informer: %v", err)
		}
	}
	if !c.WaitForCacheSync(ctx) {
		klog.Fatal("cache did not sync")
	}

	a.WithCustomMetrics(&metricsadapter.Provider{
		Reader:      c,
		MinCoverage: a.minCoverage,
	})

	if err := a.Run(ctx); err != nil {
		klog.Fatalf("unable to run adapter: %v", err)
	}
}
